"""睡眠整理、可回溯梦境与苏醒回顾。

睡眠不代表事实生成权限：LLM 逐批审阅原话，产物保存在待复核资料中；
梦永远是独立的 generated_fiction。真实记忆只允许有限、可逆的检索强度
调整，不修改重要性、使用次数、事实正文或认识论状态。

所有慢速 LLM 调用由宿主在事务外完成。本模块冻结来源指纹、发放批次
租约，再在提交时复核；崩溃后已完成批次保留，过期批次可重新领取。
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import timedelta
import json
import math
import random
import secrets
from types import SimpleNamespace

from .clock import ensure_utc, from_utc_iso, to_utc_iso
from .llm import AdapterError, EstimatedTokenCounter
from .models import MemoryStatus, Scope, new_id
from .references import ViewReader
from .storage.repository import _scope_key


SLEEP_GUIDANCE = (
    "这是用户明确触发的睡眠整理。按 cursor 读取并审阅全部本地批次；"
    "区分用户自述、助手生成、愿望、引述和虚构。按工作、情景、语义、程序"
    "四类写简短待复核笔记，逐批回传准确的 source_event_ids；不得将笔记"
    "或梦写成已发生事实，不得自动改人格核心。所有批次完成后才报告整理完成。"
    "梦是来源碎片的虚构重组；可讲给用户听，但不可作为共同经历的证据。"
)


class SleepError(ValueError):
    """可预期的睡眠协议失败，code 供可信宿主决定重试或重新准备。"""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _text(value, name, limit=200):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise SleepError("INVALID_ARGUMENT", f"{name} 必须为 1..{limit} 字符")
    return value


def _integer(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise SleepError("INVALID_ARGUMENT", f"{name} 必须为 {low}..{high} 整数")
    return value


class SleepService:
    """一个 Runtime 的睡眠领域服务；创建服务本身不打开线程或调用模型。"""

    def __init__(self, runtime):
        self.runtime = runtime
        self.db = runtime.db
        self.conn = runtime.db.connection
        self.repos = runtime.db.repositories
        # 缓存只记“哪一代内容已逐来源验证”，不存正文或跨进程持久化。
        # 所有主数据写入由数据库触发器递增 generation；睡眠审阅写入不会
        # 改变事实，因此可避免每个小分片重复全会话扫描。
        self._validated = OrderedDict()

    def _remember_validation(self, scope, row):
        if self.db._depth != 1:
            return  # 外层事务可能回滚，不能缓存未提交主数据的验证结果。
        now = to_utc_iso(self.runtime.clock.now())
        # 有效期越界会改变 ViewReader 的 valid_now，但不会产生数据库写入。
        # 用同范围最早边界作保守失效时间，避免缓存漏掉这类版本变化。
        expiry = self.conn.execute(
            "SELECT MIN(boundary) FROM (SELECT MIN(valid_from) AS boundary FROM memories "
            "WHERE user_id=? AND character_id=? AND relationship_id IS ? AND domain=? AND valid_from>? "
            "UNION ALL SELECT MIN(valid_to) AS boundary FROM memories "
            "WHERE user_id=? AND character_id=? AND relationship_id IS ? AND domain=? AND valid_to>?)",
            (scope.user_id, scope.character_id, scope.relationship_id, scope.domain, now,
             scope.user_id, scope.character_id, scope.relationship_id, scope.domain, now),
        ).fetchone()[0]
        self._validated[row["sleep_id"]] = (_scope_key(scope), row["scope_epoch"],
            self.repos.retrieval_cache.generation(), now, expiry)
        self._validated.move_to_end(row["sleep_id"])
        while len(self._validated) > 64:
            self._validated.popitem(last=False)

    def _reader(self, scope):
        # 不使用宿主会话随机引用。睡眠来源 ID 属于可信内部协议，读取仍沿用
        # 同一 ViewReader 的权限、片段遮蔽和逐跳证据保护。
        binding = SimpleNamespace(user_id=scope.user_id, character_id=scope.character_id,
            relationship_id=scope.relationship_id, domain=scope.domain,
            allowed_activity_ids=(() if scope.activity_id is None else (scope.activity_id,)), scope=scope)
        return ViewReader(self.runtime, binding, self.runtime.clock)

    def _view(self, scope, kind, source_id):
        return self._reader(scope).load(kind, source_id)

    def _fingerprint(self, value):
        return self.repos.forgetting.fingerprint(_json(value))

    def _load(self, scope, sleep_id):
        if not isinstance(scope, Scope):
            raise SleepError("INVALID_ARGUMENT", "scope 必须由可信宿主绑定")
        _text(sleep_id, "sleep_id")
        row = self.conn.execute("SELECT * FROM sleep_runs WHERE sleep_id=? AND scope_key=?",
                                (sleep_id, _scope_key(scope))).fetchone()
        if row is None:
            raise SleepError("NOT_FOUND_OR_FORBIDDEN", "找不到绑定范围的睡眠记录")
        return row

    def _invalidate(self, row, reason):
        # 撤销时清除生成正文与审阅内容，避免后续检查接口意外返回旧片段。
        self.conn.execute("UPDATE sleep_runs SET status='invalidated',invalidated_reason=?,completion_json=NULL WHERE sleep_id=?",
                          (reason, row["sleep_id"]))
        self.conn.execute("UPDATE dreams SET status='withdrawn',text='' WHERE sleep_id=?", (row["sleep_id"],))
        self.conn.execute("UPDATE sleep_batches SET review_json=NULL WHERE sleep_id=?", (row["sleep_id"],))
        self._validated.pop(row["sleep_id"], None)

    def _fresh(self, scope, row):
        if row["status"] == "invalidated":
            return False
        if self.repos.forgetting.epoch(scope) != row["scope_epoch"]:
            self._invalidate(row, "SCOPE_REVISION_CHANGED")
            return False
        generation = self.repos.retrieval_cache.generation()
        cached = self._validated.get(row["sleep_id"]) if self.db._depth == 1 else None
        now = to_utc_iso(self.runtime.clock.now())
        if (cached and cached[:3] == (_scope_key(scope), row["scope_epoch"], generation)
                and now >= cached[3] and (cached[4] is None or now < cached[4])):
            self._validated.move_to_end(row["sleep_id"])
            return True
        for source in self.conn.execute("SELECT * FROM sleep_sources WHERE sleep_id=?", (row["sleep_id"],)).fetchall():
            try:
                view = self._view(scope, source["kind"], source["source_id"])
                if view.revision != source["revision"]:
                    raise AdapterError("STALE_REFERENCE", "睡眠来源已变化")
            except AdapterError:
                self._invalidate(row, "SOURCE_REVISION_CHANGED")
                return False
        self._remember_validation(scope, row)
        return True

    def _invalid_receipt(self, row):
        return {"sleep_id": row["sleep_id"], "status": "invalidated", "dream": None,
                "review_complete": False, "warnings": ["睡眠来源或遗忘状态已变化，请重新睡眠整理。"]}

    def _progress(self, row):
        counts = {r["status"]: r["n"] for r in self.conn.execute(
            "SELECT status,COUNT(*) AS n FROM sleep_batches WHERE sleep_id=? GROUP BY status", (row["sleep_id"],))}
        cursor = self.conn.execute("SELECT MIN(cursor) FROM sleep_batches WHERE sleep_id=? AND status!='reviewed'",
                                   (row["sleep_id"],)).fetchone()[0]
        return {"batch_count": sum(counts.values()), "reviewed_batches": counts.get("reviewed", 0), "next_cursor": cursor}

    def _prepared(self, row):
        return {"sleep_id": row["sleep_id"], "status": row["status"],
                "inventory": json.loads(row["inventory_json"]), "review": self._progress(row),
                "guidance": SLEEP_GUIDANCE, "review_complete": bool(row["review_complete"]),
                "generated_facts": False}

    def prepare(self, scope: Scope, conversation_id: str, request_id: str, *,
                batch_chars: int = 5000, fragment_chars: int = 3000, max_events: int = 50000):
        """冻结本窗口全部可读取事件和它们形成的有效记忆，不调用 LLM。

        只存来源 ID/指纹和分片坐标，不复制原始全文进工作队列；超过上限
        明确失败，不把“只读了最后几条”宣称为整理了整个本地会话。
        """
        if not isinstance(scope, Scope):
            raise SleepError("INVALID_ARGUMENT", "scope 必须由可信宿主绑定")
        _text(conversation_id, "conversation_id")
        _text(request_id, "request_id")
        _integer(batch_chars, "batch_chars", 512, 12000)
        _integer(fragment_chars, "fragment_chars", 128, min(batch_chars, 4000))
        _integer(max_events, "max_events", 1, 50000)
        request_key = self._fingerprint([batch_chars, fragment_chars, max_events])
        with self.db.transaction():
            previous = self.conn.execute("SELECT * FROM sleep_runs WHERE scope_key=? AND conversation_id=? AND request_id=?",
                (_scope_key(scope), conversation_id, request_id)).fetchone()
            if previous:
                if previous["request_key"] != request_key:
                    raise SleepError("IDEMPOTENCY_CONFLICT", "同一睡眠请求不能改变分批参数")
                if not self._fresh(scope, previous):
                    return self._invalid_receipt(previous)
                return self._prepared(previous)
            events = self.conn.execute(
                "SELECT event_id,speaker FROM events WHERE user_id=? AND character_id=? AND relationship_id IS ? "
                "AND activity_id IS ? AND domain=? AND conversation_id=? ORDER BY occurred_at,recorded_at,event_id LIMIT ?",
                (scope.user_id, scope.character_id, scope.relationship_id, scope.activity_id, scope.domain,
                 conversation_id, max_events+1),
            ).fetchall()
            if len(events) > max_events:
                raise SleepError("SLEEP_INVENTORY_TOO_LARGE", "会话超过本次睡眠清点上限，需显式分段整理")
            sleep_id, now = new_id("sleep"), self.runtime.clock.now()
            source_records, fragments, counts = [], [], {"user": 0, "character": 0, "system": 0}
            unavailable = 0
            for ordinal, event in enumerate(events):
                counts[event["speaker"]] = counts.get(event["speaker"], 0)+1
                try:
                    view = self._view(scope, "event", event["event_id"])
                except AdapterError:
                    unavailable += 1
                    continue
                text = view.data["text"]
                source_records.append((sleep_id, "event", event["event_id"], view.revision, ordinal))
                for start in range(0, len(text), fragment_chars):
                    fragments.append({"event_id": event["event_id"], "start": start,
                                      "end": min(start+fragment_chars, len(text)), "total": len(text)})
            event_ids = {row[2] for row in source_records}
            memory_types = {kind: 0 for kind in ("working", "episodic", "semantic", "procedural")}
            for item in self.repos.memories.list_by_scope(scope, limit=-1, statuses=(MemoryStatus.ACTIVE,)):
                if item.scope != scope or not event_ids.intersection(item.source_event_ids):
                    continue
                try:
                    view = self._view(scope, "memory", item.memory_id)
                except AdapterError:
                    continue
                source_records.append((sleep_id, "memory", item.memory_id, view.revision, len(source_records)))
                memory_types[item.memory_type.value] += 1
            inventory = {"conversation_id": conversation_id, "event_count": len(events),
                "readable_events": len(event_ids), "excluded_events": unavailable,
                "event_speakers": counts, "memory_types": memory_types, "all_local_events_inventoried": True}
            self.conn.execute("INSERT INTO sleep_runs(sleep_id,scope_key,user_id,character_id,relationship_id,activity_id,domain,"
                "conversation_id,request_id,request_key,scope_epoch,seed,status,inventory_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sleep_id, _scope_key(scope), scope.user_id, scope.character_id, scope.relationship_id, scope.activity_id,
                 scope.domain, conversation_id, request_id, request_key, self.repos.forgetting.epoch(scope),
                 secrets.token_hex(16), "prepared", _json(inventory), to_utc_iso(now)))
            self.conn.executemany("INSERT INTO sleep_sources VALUES(?,?,?,?,?)", source_records)
            batches, pending, size = [], [], 0
            for fragment in fragments:
                length = fragment["end"]-fragment["start"]
                if pending and (size+length > batch_chars or len(pending) >= 12):
                    batches.append(pending)
                    pending, size = [], 0
                pending.append(fragment)
                size += length
            if pending:
                batches.append(pending)
            for cursor, batch in enumerate(batches):
                self.conn.execute("INSERT INTO sleep_batches(sleep_id,cursor,fragments_json) VALUES(?,?,?)",
                                  (sleep_id, cursor, _json(batch)))
            self.repos.journal.append("sleep.prepared", scope=scope, entity_id=sleep_id,
                payload={"events": len(events), "batches": len(batches)}, now=now)
            prepared = self._load(scope, sleep_id)
            self._remember_validation(scope, prepared)
            return self._prepared(prepared)

    def batch(self, scope, sleep_id, cursor=0, *, request_id=None, lease_seconds=120):
        """领取一个有界审阅分片；LLM 在此函数返回后的事务外执行。"""
        _integer(cursor, "cursor", 0, 1000000)
        _integer(lease_seconds, "lease_seconds", 5, 600)
        if request_id is not None:
            _text(request_id, "request_id")
        with self.db.transaction():
            run = self._load(scope, sleep_id)
            if not self._fresh(scope, run):
                return self._invalid_receipt(run)
            row = self.conn.execute("SELECT * FROM sleep_batches WHERE sleep_id=? AND cursor=?", (sleep_id, cursor)).fetchone()
            if row is None:
                raise SleepError("NOT_FOUND_OR_FORBIDDEN", "睡眠分片不存在")
            now = self.runtime.clock.now()
            claim_token = row["claim_token"]
            if row["status"] != "reviewed":
                if run["status"] == "completed":
                    raise SleepError("SLEEP_ALREADY_COMPLETED", "睡眠已完成，不能新增审阅")
                if row["status"] == "running" and from_utc_iso(row["lease_until"]) > ensure_utc(now):
                    if request_id is None or request_id != row["claim_request_id"]:
                        raise SleepError("SLEEP_BATCH_BUSY", "分片正在被另一审阅者处理，等待租约或使用原请求重试")
                else:
                    claim_token = secrets.token_urlsafe(18)
                    self.conn.execute("UPDATE sleep_batches SET status='running',claim_token=?,claim_request_id=?,lease_until=? "
                        "WHERE sleep_id=? AND cursor=?", (claim_token, request_id, to_utc_iso(now+timedelta(seconds=lease_seconds)), sleep_id, cursor))
                self.conn.execute("UPDATE sleep_runs SET status='collecting' WHERE sleep_id=?", (sleep_id,))
            events = []
            for fragment in json.loads(row["fragments_json"]):
                view = self._view(scope, "event", fragment["event_id"])
                events.append({"event_id": fragment["event_id"], "speaker": view.data["speaker"],
                    "content": view.data["text"][fragment["start"]:fragment["end"]],
                    "part": {name: fragment[name] for name in ("start", "end", "total")}})
            next_cursor = self.conn.execute("SELECT MIN(cursor) FROM sleep_batches WHERE sleep_id=? AND cursor>?", (sleep_id, cursor)).fetchone()[0]
            return {"sleep_id": sleep_id, "batch_id": f"{sleep_id}:{cursor}", "cursor": cursor,
                    "next_cursor": next_cursor, "events": events, "source_event_ids": sorted({event["event_id"] for event in events}),
                    "claim_token": claim_token, "reviewed": row["status"] == "reviewed", "guidance": SLEEP_GUIDANCE}

    read_batch = batch

    def record_review(self, scope, sleep_id, cursor, notes, source_event_ids, *, claim_token=None, request_id=None):
        """接受结构化审阅回执，不把 LLM 整理笔记提升为事实或人格设定。"""
        _integer(cursor, "cursor", 0, 1000000)
        if request_id is not None:
            _text(request_id, "request_id")
        if not isinstance(notes, list) or len(notes) > 12:
            raise SleepError("INVALID_ARGUMENT", "notes 必须为最多 12 项的短笔记")
        if not isinstance(source_event_ids, list) or len(source_event_ids) > 12 or any(not isinstance(key, str) for key in source_event_ids):
            raise SleepError("INVALID_ARGUMENT", "source_event_ids 无效")
        allowed = set(source_event_ids)
        normalized = []
        for note in notes:
            if isinstance(note, str):
                _text(note, "note", 1000)
                normalized.append({"text": note, "epistemic_status": "candidate_review", "source_event_ids": sorted(allowed)})
            elif isinstance(note, dict):
                if set(note) - {"memory_type", "text", "source_event_ids", "epistemic_status"}:
                    raise SleepError("INVALID_ARGUMENT", "审阅笔记包含未知字段")
                _text(note.get("text"), "note.text", 1000)
                if note.get("memory_type") not in ("working", "episodic", "semantic", "procedural"):
                    raise SleepError("INVALID_ARGUMENT", "审阅 memory_type 必须为四类之一")
                if note.get("epistemic_status", "candidate_review") != "candidate_review":
                    raise SleepError("INVALID_ARGUMENT", "整理产物只能标记 candidate_review")
                refs = note.get("source_event_ids")
                if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in allowed for ref in refs):
                    raise SleepError("INVALID_ARGUMENT", "审阅笔记必须引用本分片真实来源")
                normalized.append({**note, "epistemic_status": "candidate_review", "source_event_ids": sorted(set(refs))})
            else:
                raise SleepError("INVALID_ARGUMENT", "审阅笔记必须是短文本或类型化对象")
        if len(_json(normalized)) > 6000:
            raise SleepError("INVALID_ARGUMENT", "审阅产物超过 6000 字符")
        review_key = self._fingerprint([normalized, sorted(allowed)])
        with self.db.transaction():
            run = self._load(scope, sleep_id)
            if not self._fresh(scope, run):
                return self._invalid_receipt(run)
            batch = self.conn.execute("SELECT * FROM sleep_batches WHERE sleep_id=? AND cursor=?", (sleep_id, cursor)).fetchone()
            if batch is None:
                raise SleepError("NOT_FOUND_OR_FORBIDDEN", "睡眠分片不存在")
            expected = {f["event_id"] for f in json.loads(batch["fragments_json"])}
            if allowed != expected:
                raise SleepError("SLEEP_COVERAGE_MISMATCH", "审阅回执必须列出本分片的全部来源，不得漏项或增加来源")
            if batch["status"] == "reviewed":
                if batch["review_key"] != review_key:
                    raise SleepError("IDEMPOTENCY_CONFLICT", "已提交的审阅不能被不同文本改写")
                return {"sleep_id": sleep_id, "cursor": cursor, "status": "reviewed", "duplicate": True,
                        "review": self._progress(run), "epistemic_status": "candidate_review"}
            now = self.runtime.clock.now()
            if run["status"] == "completed":
                raise SleepError("SLEEP_ALREADY_COMPLETED", "睡眠已完成，不能提交迟到审阅")
            if (batch["status"] != "running" or claim_token != batch["claim_token"]
                    or from_utc_iso(batch["lease_until"]) <= ensure_utc(now)):
                raise SleepError("SLEEP_LEASE_LOST", "分片审阅租约无效，旧结果不得覆盖新结果")
            self.conn.execute("UPDATE sleep_batches SET status='reviewed',review_key=?,review_json=?,reviewed_at=? WHERE sleep_id=? AND cursor=?",
                (review_key, _json({"notes": normalized, "source_event_ids": sorted(allowed), "epistemic_status": "candidate_review"}),
                 to_utc_iso(now), sleep_id, cursor))
            return {"sleep_id": sleep_id, "cursor": cursor, "status": "reviewed", "duplicate": False,
                    "review": self._progress(run), "epistemic_status": "candidate_review"}

    def _dream_fragments(self, scope, run):
        choices = []
        now = ensure_utc(self.runtime.clock.now())
        for source in self.conn.execute("SELECT * FROM sleep_sources WHERE sleep_id=? ORDER BY kind,ordinal", (run["sleep_id"],)).fetchall():
            view = self._view(scope, source["kind"], source["source_id"])
            item = self.repos.memories.get(source["source_id"]) if source["kind"] == "memory" else None
            if item is not None:
                salience, moment, text = item.importance, item.occurred_start or item.recorded_at, item.render(140)
            else:
                event = self.repos.events.get(source["source_id"])
                salience, moment, text = .25, event.occurred_at, view.data["text"][:140]
            if not text.strip():
                continue
            age = max(0, (now-ensure_utc(moment)).total_seconds()/3600)
            weight = .2+.6*salience+.2*math.exp(-age/48)
            choices.append({"kind": source["kind"], "source_id": source["source_id"], "revision": source["revision"],
                            "text": text, "salience": salience, "weight": weight})
        rng = random.Random(run["seed"])
        # 指数竞争采样等价于带权无放回抽样；重要与低重要片段各保留一席，
        # 使梦可以跨显著性重组，但不把“梦到过”当成真实性证据。
        for choice in choices:
            choice["rank"] = -math.log(max(rng.random(), 1e-15))/choice["weight"]
        ranked = sorted(choices, key=lambda value: value["rank"])
        selected = []
        for candidates in ([v for v in ranked if v["salience"] >= .65], [v for v in ranked if v["salience"] <= .35], ranked):
            for choice in candidates:
                if choice not in selected:
                    selected.append(choice)
                    if candidates is not ranked:
                        break
                if len(selected) >= 6:
                    break
            if len(selected) >= 6:
                break
        rng.shuffle(selected)
        return selected

    def complete(self, scope, sleep_id, *, review_notes=None, reviewed_event_ids=None,
                 allow_partial=False, request_id=None, dream_text=None):
        """复核全量批次并发布梦与可逆强度调整，整个发布只有一个事务。

        allow_partial 是可信宿主的显式降级选择；报告保留未完成审阅数，
        不能因为存在模板梦就宣称当前登录模型已穷尽全部本地对话。
        """
        if not isinstance(allow_partial, bool):
            raise SleepError("INVALID_ARGUMENT", "allow_partial 必须为布尔值")
        if request_id is not None:
            _text(request_id, "request_id")
        if dream_text is not None:
            _text(dream_text, "dream_text", 3000)
        if review_notes not in (None, []):
            raise SleepError("SLEEP_BATCH_REVIEW_REQUIRED", "整理笔记必须逐批通过 record_review 提交，不能用单份笔记代替全部来源")
        if reviewed_event_ids is not None and (not isinstance(reviewed_event_ids, list)
                or any(not isinstance(key, str) for key in reviewed_event_ids)):
            raise SleepError("INVALID_ARGUMENT", "reviewed_event_ids 必须为真实事件 ID 列表")
        completion_key = self._fingerprint([allow_partial, dream_text, sorted(reviewed_event_ids or [])])
        with self.db.transaction():
            run = self._load(scope, sleep_id)
            if not self._fresh(scope, run):
                return self._invalid_receipt(run)
            if run["status"] == "completed":
                if run["completion_key"] != completion_key:
                    raise SleepError("IDEMPOTENCY_CONFLICT", "已完成的睡眠不能用不同产物重复发布")
                return {**json.loads(run["completion_json"]), "duplicate": True}
            progress = self._progress(run)
            all_reviewed = progress["reviewed_batches"] == progress["batch_count"]
            if not all_reviewed and not allow_partial:
                raise SleepError("SLEEP_REVIEW_INCOMPLETE", "仍有本地对话分片未审阅，不能报告睡眠整理完成")
            if reviewed_event_ids is not None:
                expected = {r[0] for r in self.conn.execute("SELECT source_id FROM sleep_sources WHERE sleep_id=? AND kind='event'", (sleep_id,))}
                if set(reviewed_event_ids) != expected or not all_reviewed:
                    raise SleepError("SLEEP_COVERAGE_MISMATCH", "全量审阅声明与持久分片回执不一致")
            fragments = self._dream_fragments(scope, run)
            if dream_text is None:
                if fragments:
                    joined = "；忽然，".join(f"「{fragment['text']}」" for fragment in fragments)
                    dream_text = f"梦里，{joined}。这些片段像纸船一样漂到一起，最后停在一扇发光的窗前。"
                else:
                    dream_text = "梦里是一片尚未写下故事的空白，只有一盏灯安静地亮着。"
                generator = "weighted-fragment-template-v1"
            else:
                generator = "host-llm-generated-fiction-v1"
            dream_id, now = new_id("dream"), self.runtime.clock.now()
            source_refs = [{name: f[name] for name in ("kind", "source_id", "revision")} for f in fragments]
            self.conn.execute("INSERT INTO dreams(dream_id,sleep_id,scope_key,text,source_refs_json,generator,created_at) VALUES(?,?,?,?,?,?,?)",
                (dream_id, sleep_id, _scope_key(scope), dream_text, _json(source_refs), generator, to_utc_iso(now)))
            selected_ids = {f["source_id"] for f in fragments if f["kind"] == "memory"}
            reinforced, decayed = [], []
            # 单次睡眠最多调整 128 条，长期记忆正文和认识论不变。仅在完整
            # 审阅时重放/衰减，部分失败的模板回退不更新真实记忆权重。
            if all_reviewed:
                ids = sorted(selected_ids)
                selected = "m.memory_id IN ("+",".join("?" for _ in ids)+")" if ids else "0"
                candidates = self.conn.execute(
                    "SELECT s.source_id FROM sleep_sources s JOIN memories m ON m.memory_id=s.source_id "
                    "WHERE s.sleep_id=? AND s.kind='memory' AND ("+selected+
                    " OR (m.importance<=.35 AND m.access_count=0 AND m.recorded_at<=?)) "
                    "ORDER BY CASE WHEN "+selected+" THEN 0 ELSE 1 END,s.ordinal LIMIT 128",
                    [sleep_id, *ids, to_utc_iso(now-timedelta(hours=48)), *ids],
                ).fetchall()
                for source in candidates:
                    item = self.repos.memories.get(source[0])
                    before, after, reason = item.retrieval_strength, item.retrieval_strength, None
                    age = (ensure_utc(now)-ensure_utc(item.recorded_at)).total_seconds()/3600
                    if item.memory_id in selected_ids:
                        # 同一事实反复发睡眠指令不能刷高召回；每24小时最多
                        # 一次回放增益。此参数是工程政策，不宣称生理精确模型。
                        replayed = self.conn.execute(
                            "SELECT 1 FROM sleep_weight_changes w JOIN sleep_runs r USING(sleep_id) "
                            "WHERE w.memory_id=? AND w.reason='source_replay' AND w.restored_at IS NULL "
                            "AND r.completed_at>? LIMIT 1", (item.memory_id, to_utc_iso(now-timedelta(hours=24))),
                        ).fetchone()
                        if not replayed:
                            after, reason = max(before, min(.95, before+.04)), "source_replay"
                    elif item.importance <= .35 and item.access_count == 0 and age >= 48:
                        after, reason = min(before, max(.15, before*.96)), "low_salience_decay"
                    if reason is None or abs(after-before) < 1e-12:
                        continue
                    self.conn.execute("UPDATE memories SET retrieval_strength=? WHERE memory_id=?", (after, item.memory_id))
                    self.conn.execute("INSERT INTO sleep_weight_changes VALUES(?,?,?,?,?,NULL)", (sleep_id, item.memory_id, before, after, reason))
                    (reinforced if reason == "source_replay" else decayed).append(item.memory_id)
            receipt = {"sleep_id": sleep_id, "status": "completed", "review_complete": all_reviewed,
                "review": progress, "inventory": json.loads(run["inventory_json"]), "duplicate": False,
                "dream": {"dream_id": dream_id, "kind": "dream", "epistemic_status": "generated_fiction",
                          "modality": "fictional", "text": dream_text, "generator": generator, "source_refs": source_refs},
                "consolidation": {"reinforced_memory_ids": reinforced, "decayed_memory_ids": decayed,
                                  "facts_created": 0, "core_changes": 0, "physical_deletions": 0,
                                  "meaning": "检索可用性的有限调整，不代表真实性增加。"},
                "warnings": [] if all_reviewed else ["部分本地对话尚未完成模型审阅；梦使用显式降级，未调整真实记忆权重。"]}
            self.conn.execute("UPDATE sleep_runs SET status='completed',review_complete=?,completion_key=?,completion_json=?,completed_at=? WHERE sleep_id=?",
                              (all_reviewed, completion_key, _json(receipt), to_utc_iso(now), sleep_id))
            self.repos.journal.append("sleep.completed", scope=scope, entity_id=sleep_id,
                payload={"review_complete": all_reviewed, "dream_id": dream_id, "reinforced": len(reinforced), "decayed": len(decayed)}, now=now)
            return receipt

    def wake(self, scope, conversation_id, request_id, *, max_tokens=1200):
        """跨窗口读取同一精确范围的最近梦；每次重新核验，不重写用户事实。"""
        _text(conversation_id, "conversation_id")
        _text(request_id, "request_id")
        _integer(max_tokens, "max_tokens", 128, 4096)
        with self.db.transaction():
            rows = self.conn.execute("SELECT * FROM sleep_runs WHERE scope_key=? AND status='completed' ORDER BY completed_at DESC,rowid DESC LIMIT 20",
                                     (_scope_key(scope),)).fetchall()
            for row in rows:
                if not self._fresh(scope, row):
                    continue
                dream = self.conn.execute("SELECT * FROM dreams WHERE sleep_id=? AND status='active'", (row["sleep_id"],)).fetchone()
                if dream is None:
                    continue
                result = {"status": "awake", "sleep_id": row["sleep_id"], "conversation_id": conversation_id,
                    "review_complete": bool(row["review_complete"]), "dream": {"dream_id": dream["dream_id"],
                    "kind": "dream", "epistemic_status": "generated_fiction", "modality": "fictional",
                    "text": dream["text"], "source_refs": json.loads(dream["source_refs_json"])},
                    "guidance": "可以把这个梦作为虚构梦境讲述；它不是实际共同经历，不可作为事实或核心依据。", "truncated": False}
                # 只给苏醒一个短候选回顾，不把几千批笔记重新塞回新窗口。
                # 候选标签继续保留；后续正式事实恢复仍走普通检索和来源核验。
                notes = []
                for review in self.conn.execute(
                    "SELECT review_json FROM sleep_batches WHERE sleep_id=? AND status='reviewed' "
                    "AND review_json IS NOT NULL ORDER BY cursor DESC LIMIT 4", (row["sleep_id"],),
                ):
                    notes.extend(json.loads(review[0]).get("notes", []))
                if notes:
                    result["review_summary"] = {"epistemic_status": "candidate_review", "approved_facts": False,
                        "notes": [{**note, "text": note["text"][:240]} for note in notes[:4]]}
                counter = EstimatedTokenCounter()
                while counter.count(_json(result)) > max_tokens:
                    if result.get("review_summary", {}).get("notes"):
                        result["review_summary"]["notes"].pop()
                        result["truncated"] = True
                        continue
                    if "review_summary" in result:
                        del result["review_summary"]
                        continue
                    text = result["dream"]["text"]
                    if len(text) <= 40:
                        result["dream"] = {"dream_id": dream["dream_id"], "kind": "dream", "epistemic_status": "generated_fiction",
                                           "modality": "fictional", "text": "梦的内容超出本次预算，可增加预算后读取。"}
                        result["truncated"] = True
                        break
                    result["dream"]["text"] = text[:max(32, len(text)//2)]+"…"
                    result["truncated"] = True
                if counter.count(_json(result)) > max_tokens:
                    # 最小预算优先保留虚构标签和明确截断，不能无限截断同一短串。
                    result = {"status": "awake", "dream": {"kind": "dream", "epistemic_status": "generated_fiction",
                        "modality": "fictional", "text": "梦的完整回顾超出本次预算，可增加预算后读取。"},
                        "review_complete": bool(row["review_complete"]), "truncated": True}
                return result
            return {"status": "awake", "conversation_id": conversation_id, "dream": None,
                    "review_complete": False, "warnings": ["没有可验证的已完成睡眠梦境；可能尚未睡眠或来源已撤销。"]}

    def restore_weights(self, scope, sleep_id):
        """可信宿主撤销本次强度调整；不覆盖后来独立变化的权重。"""
        with self.db.transaction():
            run = self._load(scope, sleep_id)
            restored, skipped = [], []
            for row in self.conn.execute("SELECT * FROM sleep_weight_changes WHERE sleep_id=? AND restored_at IS NULL", (sleep_id,)).fetchall():
                item = self.repos.memories.get(row["memory_id"])
                source = self.conn.execute("SELECT revision FROM sleep_sources WHERE sleep_id=? AND kind='memory' AND source_id=?",
                                           (sleep_id, row["memory_id"])).fetchone()
                try:
                    still_same = source is not None and self._view(scope, "memory", row["memory_id"]).revision == source[0]
                except AdapterError:
                    still_same = False
                if (item is None or item.scope != scope or item.status is not MemoryStatus.ACTIVE or not still_same
                        or abs(item.retrieval_strength-row["after_strength"]) > 1e-12):
                    skipped.append(row["memory_id"])
                    continue
                self.conn.execute("UPDATE memories SET retrieval_strength=? WHERE memory_id=?", (row["before_strength"], row["memory_id"]))
                self.conn.execute("UPDATE sleep_weight_changes SET restored_at=? WHERE sleep_id=? AND memory_id=?",
                                  (to_utc_iso(self.runtime.clock.now()), sleep_id, row["memory_id"]))
                restored.append(row["memory_id"])
            return {"sleep_id": run["sleep_id"], "restored_memory_ids": restored, "skipped_memory_ids": skipped}

    def status(self, scope, sleep_id):
        """可信宿主的进度入口；只返回清点计数，不返回未经复核的历史正文。"""
        with self.db.transaction():
            run = self._load(scope, sleep_id)
            if not self._fresh(scope, run):
                return self._invalid_receipt(run)
            return self._prepared(run)

    def invalidate_stale(self, scope=None):
        """供遗忘/纠错提交链调用；范围清除直接删除 sleep_runs 并级联所有派生物。"""
        with self.db.transaction():
            sql = "SELECT * FROM sleep_runs WHERE status!='invalidated'"
            args = ()
            if scope is not None:
                sql += " AND scope_key=?"
                args = (_scope_key(scope),)
            invalidated = []
            for row in self.conn.execute(sql, args).fetchall():
                actual = Scope(row["user_id"], row["character_id"], row["relationship_id"], row["activity_id"], row["domain"])
                if not self._fresh(actual, row):
                    invalidated.append(row["sleep_id"])
            return invalidated
