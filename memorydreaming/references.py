"""会话内随机引用及逐跳来源校验。此模块不提供身份认证，binding 来自可信宿主。"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import timedelta
from functools import cached_property

from .clock import to_utc_iso
from .llm import AdapterError, dumps
from .models import CoreKind, LinkRelation, MemoryStatus, Scope


def forbidden() -> AdapterError:
    return AdapterError("NOT_FOUND_OR_FORBIDDEN", "找不到当前绑定可读取的引用")


def stale() -> AdapterError:
    return AdapterError("STALE_REFERENCE", "引用已变化，请重新查询", next_action="refresh_reference")


@dataclass
class RecordView:
    kind: str
    target: str
    data: dict
    epoch: int
    sources: list["RecordView"] = field(default_factory=list)
    fields: dict[str, list["RecordView"]] = field(default_factory=dict)

    @cached_property
    def revision(self) -> str:
        value = [self.kind, self.target, self.data, self.epoch,
                 [(item.kind, item.target, item.revision) for item in self.sources]]
        return hashlib.sha256(dumps(value).encode("utf-8")).hexdigest()


class ReferenceRegistry:
    """只存目标与摘要，避免在引用缓存中再复制敏感正文。关闭窗口后引用不可复用。"""

    def __init__(self, clock, *, ttl_seconds=1800, capacity=2048):
        self.clock, self.ttl_seconds, self.capacity = clock, ttl_seconds, capacity
        self._entries: OrderedDict[str, tuple[str, str, str, object]] = OrderedDict()
        self._reverse: dict[tuple[str, str, str], str] = {}

    def issue(self, view: RecordView) -> str:
        key = (view.kind, view.target, view.revision)
        existing = self._reverse.get(key)
        if existing in self._entries and self._entries[existing][3] > self.clock.now():
            self._entries.move_to_end(existing)
            return existing
        ref = ("aref_" if view.kind == "activity" else "mref_") + secrets.token_urlsafe(18)
        self._entries[ref] = (*key, self.clock.now() + timedelta(seconds=self.ttl_seconds))
        self._reverse[key] = ref
        while len(self._entries) > self.capacity:
            old_ref, entry = self._entries.popitem(last=False)
            if self._reverse.get(entry[:3]) == old_ref:
                self._reverse.pop(entry[:3], None)
        return ref

    def resolve(self, refs: list[str], reader: "ViewReader", *, activity=False) -> list[RecordView]:
        # 先检查整批所有句柄；混有外国引用时不泄露任何已验证内容。
        entries = [self._entries.get(ref) for ref in refs]
        if any(entry is None or entry[3] <= self.clock.now() or ((entry[0] == "activity") != activity)
               for entry in entries):
            raise forbidden()
        result = []
        for kind, target, revision, _ in entries:
            try:
                view = reader.load(kind, target)
            except AdapterError:
                raise stale() from None
            if view.revision != revision:
                raise stale()
            result.append(view)
        return result


class ViewReader:
    """一次数据库快照内的可见投影；异常会使整个候选或读取请求失败。"""

    def __init__(self, runtime, binding, clock, *, exclude_event_ids=()):
        self.runtime, self.repos = runtime, runtime.db.repositories
        self.binding, self.clock = binding, clock
        self.excluded = set(exclude_event_ids)
        self._cache: dict[tuple[str, str], RecordView] = {}
        self._visiting: set[tuple[str, str]] = set()
        self.nodes = 0

    def visible(self, scope: Scope) -> bool:
        return (scope.user_id == self.binding.user_id and scope.character_id == self.binding.character_id
                and scope.relationship_id == self.binding.relationship_id and scope.domain == self.binding.domain
                and (scope.activity_id is None or scope.activity_id in self.binding.allowed_activity_ids))

    def load(self, kind: str, target: str) -> RecordView:
        key = (kind, target)
        if key in self._cache:
            return self._cache[key]
        self.nodes += 1
        if key in self._visiting or self.nodes > 512 or len(self._visiting) >= 64:
            raise forbidden()
        self._visiting.add(key)
        try:
            loader = {"memory": self._memory, "event": self._event, "summary": self._summary,
                      "core_version": self._core, "activity": self._activity}.get(kind)
            if loader is None:
                raise forbidden()
            view = loader(target)
            self._cache[key] = view
            return view
        finally:
            self._visiting.remove(key)

    def source(self, target: str) -> RecordView:
        return self.load("memory" if self.repos.memories.get(target) is not None else "event", target)

    def _memory(self, target: str) -> RecordView:
        item = self.repos.memories.get(target)
        if (item is None or not self.visible(item.scope)
                or item.status in (MemoryStatus.DELETED, MemoryStatus.RETRACTED)
                or self.repos.forgetting.is_suppressed(item)
                or (item.status is MemoryStatus.SUPERSEDED and self.repos.links.has_relation(target, LinkRelation.PART_OF))):
            raise forbidden()
        sources = [self.load("event", ref) for ref in sorted(set(item.source_event_ids))]
        sources += [self.load("memory", ref) for ref in sorted(set(self.repos.links.parents_of(target)))]
        data = self._record_data("memory", dumps(item.body), item.occurred_start, item.occurred_end, item.occurred_precision)
        data.update(memory_type=item.memory_type.value,
                    speaker=item.source_speaker if item.source_speaker in ("user", "character", "system") else None,
                    epistemic_status=item.epistemic_status.value, modality=item.modality.value,
                    valid_from=to_utc_iso(item.valid_from), valid_to=to_utc_iso(item.valid_to),
                    valid_now=item.is_current(self.clock.now()))
        return RecordView("memory", target, data, self.repos.forgetting.epoch(item.scope), sources)

    def _event(self, target: str) -> RecordView:
        item = self.repos.events.get(target)
        if (item is None or not self.visible(item.scope) or target in self.excluded
                or self.repos.forgetting.is_event_forgotten(target)):
            raise forbidden()
        rows = self.runtime.db.connection.execute(
            "SELECT s.span_start,s.span_end,t.scope_key,t.tombstone_id FROM tombstone_sources s "
            "JOIN memory_tombstones t USING(tombstone_id) WHERE s.event_id=?",
            (target,),
        ).fetchall()
        content = item.content
        intervals = []
        approved = {}
        for start, end, scope_key, tombstone_id in rows:
            if not self.visible(Scope(*json.loads(scope_key))):
                continue
            if tombstone_id not in approved:
                approved[tombstone_id] = self.repos.forgetting.relearning_spans(target, tombstone_id)
            allowed = approved[tombstone_id]
            if start is None or end is None or not 0 <= start < end <= len(content):
                if not allowed:
                    raise forbidden()  # 旧记录不能定位时整条原话不可对外展开。
                start, end = 0, len(content)
            blocked = [(start, end)]
            for left, right in allowed:
                blocked = [(a, b) for begin, finish in blocked
                           for a, b in ((begin, min(left, finish)), (max(right, begin), finish)) if a < b]
            intervals.extend(blocked)
        merged: list[list[int]] = []
        for start, end in sorted(intervals):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        for start, end in reversed(merged):
            content = content[:start] + "[已抑制片段]" + content[end:]
        sources = []
        delivery = self.runtime.db.connection.execute(
            "SELECT * FROM host_deliveries WHERE event_id=?", (target,),
        ).fetchone()
        if delivery is not None:
            if not delivery["evidence_complete"]:
                raise forbidden()
            request_event = self.repos.events.get(delivery["user_event_id"])
            if (request_event is None or not self.visible(request_event.scope)
                    or request_event.conversation_id != item.conversation_id):
                raise forbidden()
            # 助手原话可能复述已遗忘事实；有使用来源时沿链复核，不能只处理用户原文。
            for source in json.loads(delivery["evidence_refs_json"]):
                view = self.load(source["kind"], source["target"])
                if view.revision != source["revision"]:
                    raise forbidden()
                sources.append(view)
        data = self._record_data("event", content, item.occurred_at, None, item.occurred_precision)
        data["speaker"] = item.speaker if item.speaker in ("user", "character", "system") else None
        return RecordView("event", target, data, self.repos.forgetting.epoch(item.scope), sources)

    def _summary(self, target: str) -> RecordView:
        item = self.repos.summaries.get(target)
        if (item is None or item.status != "active" or not self.visible(item.scope)
                or not self.repos.summaries.is_fresh(item)):
            raise forbidden()
        if not item.source_memory_ids:
            raise forbidden()
        sources = [self.load("memory", ref) for ref in sorted(set(item.source_memory_ids))]
        sources += [self.load("event", ref) for ref in sorted(set(item.source_event_ids))]
        data = self._record_data("summary", dumps({"title": item.topic, "digest": item.digest}),
                                 item.span_start, item.span_end, "coarse")
        return RecordView("summary", target, data, self.repos.forgetting.epoch(item.scope), sources)

    def _core(self, target: str) -> RecordView:
        try:
            kind = CoreKind(target)
        except ValueError:
            raise forbidden() from None
        core = self.repos.cores.latest(self.binding.scope, kind)
        if core is None:
            raise forbidden()
        payload, fields, sources = {}, {}, {}
        for name, value in core.payload.items():
            try:
                refs = [self.source(ref) for ref in core.field_evidence_ids.get(name, core.evidence_ids)]
            except AdapterError:
                continue  # 没有可验证来源的字段不对外泄露。
            payload[name], fields[name] = value, refs
            sources.update({(ref.kind, ref.target): ref for ref in refs})
        data = self._record_data("core_version", dumps(payload), core.effective_from, None, "exact")
        data["record_version"] = core.core_version
        return RecordView("core_version", target, data, self.repos.forgetting.epoch(core.scope),
                          list(sources.values()), fields)

    def _activity(self, target: str) -> RecordView:
        item = self.repos.activities.get(target)
        if item is None or target not in self.binding.allowed_activity_ids or not self.visible(item.scope):
            raise forbidden()
        return RecordView("activity", target, {"label": item.title, "version": item.checkpoint_version,
                                              "status": item.status.value}, self.repos.forgetting.epoch(item.scope))

    @staticmethod
    def _record_data(kind, content, start, end, precision):
        if precision not in ("exact", "day", "week", "month", "year", "coarse", "unknown"):
            precision = "unknown"
        return {"record_kind": kind, "memory_type": None, "text": content, "speaker": None,
                "epistemic_status": None, "modality": None,
                "occurred": {"start": to_utc_iso(start), "end": to_utc_iso(end), "precision": precision},
                "valid_from": None, "valid_to": None, "valid_now": None}


def render_record(view: RecordView, registry: ReferenceRegistry) -> dict:
    data = dict(view.data)
    data.update(ref=registry.issue(view), source_refs=[registry.issue(item) for item in view.sources[:20]])
    # 事件/记忆尚无完整修订日志，使用 48 位内容指纹作不透明版本标识，保持 JS 安全整数范围。
    data.setdefault("record_version", int(view.revision[:12], 16) + 1)
    return data
