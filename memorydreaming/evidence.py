"""类型化 AND/OR 支持组；版本钉住的边与最小不动点求解。"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import asdict
import hashlib
import json

from .assertions import confirmation_material, core_scope_key, normalize
from .clock import to_utc_iso
from .errors import MemorySuppressed, ScopeNotFound, VersionConflict
from .models import CoreKind, EvidenceRef, MemoryStatus, Scope, new_id


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def digest(value):
    return hashlib.sha256(dumps(value).encode("utf-8")).hexdigest()


def memory_material(item):
    # 使用次数和检索权重不属于依据内容；读取不会使依赖失效。
    return [item.memory_id, asdict(item.scope), item.memory_type.value, item.body,
            item.source_event_ids, item.source_spans, item.source_speaker,
            item.epistemic_status.value, item.modality.value, item.recorded_at,
            item.occurred_start, item.occurred_end, item.occurred_precision,
            item.valid_from, item.valid_to, item.supersedes_id]


def visible(parent: Scope, child: Scope):
    return (parent.user_id == child.user_id and parent.character_id == child.character_id
            and parent.relationship_id == child.relationship_id and parent.domain == child.domain
            and parent.activity_id in (None, child.activity_id))


class EvidenceEngine:
    """每次快照内计算；不跨事务缓存正文或支持状态。"""

    def __init__(self, connection, scope: Scope, *, excluded_events=(), max_nodes=4096, max_edges=16384,
                 strict_events=False):
        from .storage.repository import ForgetRepository, MemoryRepository, EventRepository
        self.conn, self.scope = connection, scope
        self.forgetting = ForgetRepository(connection)
        self.memories, self.events = MemoryRepository(connection), EventRepository(connection)
        self.excluded = set(excluded_events)
        self.max_nodes, self.max_edges = max_nodes, max_edges
        self.strict_events = strict_events
        self.raw = {}
        self.groups = {}
        self.live = set()
        self.witness = {}
        self.complete = True

    def configured(self, memory_id):
        return self.conn.execute("SELECT * FROM evidence_sets WHERE memory_id=?", (memory_id,)).fetchone()

    def stored_groups(self, memory_id):
        rows = self.conn.execute(
            "SELECT g.position AS gp,m.position,m.kind,m.target,m.revision FROM evidence_groups g "
            "LEFT JOIN evidence_members m USING(group_id) WHERE g.memory_id=? ORDER BY g.position,m.position",
            (memory_id,),
        ).fetchall()
        groups = {}
        for row in rows:
            groups.setdefault(row["gp"], [])
            if row["kind"] is not None:
                groups[row["gp"]].append(EvidenceRef(row["kind"], row["target"], row["revision"]))
        return list(groups.values())

    def _object(self, kind, target):
        key = (kind, target)
        if key in self.raw:
            return self.raw[key]
        value = None
        if kind == "memory":
            item = self.memories.get(target)
            if (item is not None and visible(item.scope, self.scope)
                    and item.status not in (MemoryStatus.DELETED, MemoryStatus.RETRACTED)
                    and not self.forgetting.is_fact_suppressed(item)):
                value = item
        elif kind == "event":
            event = self.events.get(target)
            if (event is not None and event.scope.user_id == self.scope.user_id
                    and event.scope.character_id == self.scope.character_id
                    and event.scope.relationship_id == self.scope.relationship_id
                    and event.scope.domain == self.scope.domain and target not in self.excluded
                    and (not self.strict_events or visible(event.scope, self.scope))
                    and not self.forgetting.is_event_forgotten(target)):
                value = event
        elif kind == "core_version":
            try:
                scope_key, name, version = json.loads(target)
                core_kind = CoreKind(name)
                if scope_key != core_scope_key(self.scope, core_kind):
                    raise ValueError()
                row = self.conn.execute(
                    "SELECT * FROM core_versions WHERE scope_key=? AND kind=? AND status!='rejected' "
                    "ORDER BY core_version DESC LIMIT 1", (scope_key, name),
                ).fetchone()
                if row is not None and row["core_version"] == version:
                    from .storage.repository import _core_from_row
                    core = _core_from_row(row)
                    blocked = any(self.conn.execute(
                        "SELECT 1 FROM core_field_tombstones WHERE scope_key=? AND kind=? AND field_key=?",
                        (scope_key, name, self.forgetting.fingerprint(json.dumps(
                            [field, normalize(value)], ensure_ascii=False, sort_keys=True))),
                    ).fetchone() for field, value in core.payload.items())
                    if core.payload and not blocked:
                        value = core
            except (ValueError, TypeError):
                pass
        self.raw[key] = value
        return value

    def revision(self, kind, target):
        value = self._object(kind, target)
        if value is None:
            return None
        if kind == "event":
            return digest(confirmation_material(value))
        if kind == "memory":
            config = self.configured(target)
            return digest([memory_material(value), config["version"] if config else 0])
        return digest(asdict(value))

    def valid_ref(self, ref):
        if self.revision(ref.kind, ref.target) != ref.revision:
            return False
        if ref.kind == "event" and not visible(self._object("event", ref.target).scope, self.scope):
            return False
        # Event 引用的是整条原话；有被抑制片段时不接受它为新的完整依据。
        if ref.kind == "event" and self.conn.execute(
            "SELECT 1 FROM tombstone_sources WHERE event_id=? LIMIT 1", (ref.target,),
        ).fetchone():
            return False
        return True

    def _describe(self, node):
        kind, target = node
        value = self._object(kind, target)
        if value is None:
            return False, []
        if kind == "event":
            return True, []
        if kind == "core_version":
            sources = sorted({ref for field in value.payload
                              for ref in value.field_evidence_ids.get(field, value.evidence_ids)})
            parents = []
            for source in sources:
                parent_kind = "memory" if self.memories.get(source) is not None else "event"
                if parent_kind == "event" and self.conn.execute(
                    "SELECT 1 FROM tombstone_sources WHERE event_id=? LIMIT 1", (source,),
                ).fetchone():
                    return False, []
                parents.append((parent_kind, source))
            return not sources, [parents] if parents else []
        config = self.configured(target)
        if config is not None:
            if config["content_key"] != digest(memory_material(value)):
                return False, []
            groups = []
            for group in self.stored_groups(target):
                if group and all(self.valid_ref(ref) for ref in group):
                    groups.append([(ref.kind, ref.target) for ref in group])
            return False, groups
        # 兼容旧版 derived_from 的 OR；有链接时不能用自身互相引用充当根。
        parents = self.conn.execute(
            "SELECT to_memory_id FROM memory_links WHERE from_memory_id=? AND relation='derived_from'",
            (target,),
        ).fetchall()
        if parents:
            return False, [[("memory", row[0])] for row in parents]
        if value.source_event_ids:
            return False, [[("event", source)] for source in value.source_event_ids]
        return True, []  # 兼容可信本地直写的无来源记录，不给显式空组此待遇。

    def solve(self, roots):
        queue = deque(roots)
        ready = deque()
        dependents = defaultdict(list)
        remaining = {}
        edges = 0
        while queue:
            node = queue.popleft()
            if node in self.groups:
                continue
            if len(self.groups) >= self.max_nodes:
                self.complete = False
                break
            root, groups = self._describe(node)
            self.groups[node] = groups
            if root:
                ready.append(node)
            for index, group in enumerate(groups):
                edges += len(group)
                if edges > self.max_edges:
                    self.complete = False
                    break
                remaining[(node, index)] = len(set(group))
                for parent in sorted(set(group)):
                    dependents[parent].append((node, index))
                    queue.append(parent)
            if not self.complete:
                break
        if not self.complete:
            self.live.clear()
            return self
        # 一个节点只激活一次；无外部根的环永远不进入 ready。
        while ready:
            node = ready.popleft()
            if node in self.live:
                continue
            self.live.add(node)
            for child, index in dependents[node]:
                remaining[(child, index)] -= 1
                if remaining[(child, index)] == 0 and child not in self.live:
                    self.witness.setdefault(child, self.groups[child][index])
                    ready.append(child)
        return self


def evidence_reference(runtime, kind, target, scope):
    if kind not in ("event", "memory", "core_version") or not isinstance(target, str) or not target:
        raise ValueError("依据 kind/target 无效")
    with runtime.db.transaction(write=False):
        if kind == "core_version" and target in ("identity", "relationship"):
            key = core_scope_key(scope, CoreKind(target))
            row = runtime.db.connection.execute(
                "SELECT core_version FROM core_versions WHERE scope_key=? AND kind=? AND status!='rejected' "
                "ORDER BY core_version DESC LIMIT 1", (key, target),
            ).fetchone()
            if row is None:
                raise ScopeNotFound("当前核心版本不存在")
            target = dumps([key, target, row[0]])
        engine = EvidenceEngine(runtime.db.connection, scope, strict_events=True).solve([(kind, target)])
        if (kind, target) not in engine.live:
            raise ScopeNotFound("依据不存在、版本失效或不属于绑定作用域")
        ref = EvidenceRef(kind, target, engine.revision(kind, target))
        if not engine.valid_ref(ref):
            raise MemorySuppressed("原话含受抑制片段，请使用精确的存活记忆依据")
        return ref


def set_groups(runtime, memory_id, scope, groups, *, expected_version, reason):
    if (not isinstance(reason, str) or not reason.strip() or len(reason) > 500
            or isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0):
        raise ValueError("必须提供 reason 与非负 expected_version")
    if not isinstance(groups, (list, tuple)) or not 1 <= len(groups) <= 32:
        raise ValueError("证据组必须为 1..32 个非空组")
    normalized = []
    for group in groups:
        if not isinstance(group, (list, tuple)) or not 1 <= len(group) <= 20:
            raise ValueError("每个 AND 组必须为 1..20 项，空组无效")
        unique = {}
        for ref in group:
            if (not isinstance(ref, EvidenceRef) or ref.kind not in ("event", "memory", "core_version")
                    or not isinstance(ref.target, str) or not ref.target or len(ref.target) > 1200
                    or not isinstance(ref.revision, str) or len(ref.revision) != 64):
                raise ValueError("成员必须为已签发的类型化 EvidenceRef")
            key = (ref.kind, ref.target)
            if key in unique and unique[key] != ref:
                raise ValueError("同一依据不能在组内使用冲突版本")
            unique[key] = ref
        normalized.append(sorted(unique.values(), key=lambda r: (r.kind, r.target)))
    with runtime.db.transaction():
        engine = EvidenceEngine(runtime.db.connection, scope, strict_events=True)
        item = engine.memories.get(memory_id)
        if item is None or item.scope != scope or item.status is not MemoryStatus.ACTIVE:
            raise ScopeNotFound("只能给绑定作用域内的 active 记忆设置依据")
        config = engine.configured(memory_id)
        version = config["version"] if config else 0
        if version != expected_version:
            raise VersionConflict(expected_version, version, "证据组")
        if any(not engine.valid_ref(ref) for group in normalized for ref in group):
            raise MemorySuppressed("依据已变化或不可用，请重新读取")
        from .storage.repository import _scope_key
        conn = runtime.db.connection
        conn.execute(
            "INSERT INTO evidence_sets(memory_id,scope_key,version,content_key,reason,updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(memory_id) DO UPDATE SET version=excluded.version,content_key=excluded.content_key,"
            "reason=excluded.reason,updated_at=excluded.updated_at",
            (memory_id, _scope_key(scope), version + 1, digest(memory_material(item)), reason, to_utc_iso(runtime.clock.now())),
        )
        conn.execute("DELETE FROM evidence_groups WHERE memory_id=?", (memory_id,))
        for index, group in enumerate(normalized):
            group_id = new_id("evg")
            conn.execute("INSERT INTO evidence_groups(group_id,memory_id,position) VALUES(?,?,?)", (group_id, memory_id, index))
            for position, ref in enumerate(group):
                conn.execute("INSERT INTO evidence_members(group_id,position,kind,target,revision) VALUES(?,?,?,?,?)",
                             (group_id, position, ref.kind, ref.target, ref.revision))
        checked = EvidenceEngine(conn, scope, strict_events=True).solve([("memory", memory_id)])
        if ("memory", memory_id) not in checked.live:
            raise MemorySuppressed("支持组没有可验证的外部根，不能接受自证或循环依据")
        runtime.db.repositories.forgetting.bump(scope)
        runtime.db.repositories.summaries.invalidate_for_memories([memory_id], runtime.clock.now())
        runtime.db.repositories.journal.append("evidence.bound", scope=scope, entity_id=memory_id,
            payload={"version": version + 1, "groups": len(normalized)}, now=runtime.clock.now())
        invalidated = runtime.refresh_evidence()
        return {"memory_id": memory_id, "version": version + 1, "group_count": len(normalized),
                "supported": True, "invalidated_derived": invalidated}


def inspect(runtime, memory_id, scope):
    with runtime.db.transaction(write=False):
        engine = EvidenceEngine(runtime.db.connection, scope)
        item = engine.memories.get(memory_id)
        if item is None or item.scope != scope:
            raise ScopeNotFound("目标不属于绑定作用域")
        config = engine.configured(memory_id)
        engine.solve([("memory", memory_id)])
        return {"memory_id": memory_id, "version": config["version"] if config else 0,
                "complete": engine.complete, "supported": ("memory", memory_id) in engine.live,
                "groups": [[asdict(ref) for ref in group] for group in engine.stored_groups(memory_id)]}


def invalidate(runtime):
    """统一刷新存量显式组与其下游，跨类型失效后不静默重新激活。"""
    rows = runtime.db.connection.execute(
        "SELECT m.memory_id FROM memories m WHERE m.status IN ('active','superseded') AND "
        "(EXISTS(SELECT 1 FROM evidence_sets e WHERE e.memory_id=m.memory_id) OR "
        "EXISTS(SELECT 1 FROM memory_links l WHERE l.from_memory_id=m.memory_id AND l.relation='derived_from'))"
    ).fetchall()
    by_scope = defaultdict(list)
    for row in rows:
        item = runtime.db.repositories.memories.get(row[0])
        by_scope[item.scope].append(item.memory_id)
    invalidated = []
    for scope, ids in by_scope.items():
        engine = EvidenceEngine(runtime.db.connection, scope).solve([("memory", key) for key in ids])
        if not engine.complete:
            # 规模边界不能被解释成“所有事实均已撤回”；读取仍保守拒绝。
            continue
        for key in ids:
            if ("memory", key) not in engine.live:
                runtime.db.repositories.memories.update_status(key, MemoryStatus.RETRACTED, runtime.clock.now())
                runtime.vectors.delete(key)
                runtime.db.repositories.entities.remove_memory(key)
                invalidated.append(key)
    return sorted(invalidated)
