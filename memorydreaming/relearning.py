"""由可信宿主批准的重新学习：新证据、新记录，旧抑制不撤销。"""

from __future__ import annotations

from copy import deepcopy
import json

from .assertions import confirmation_material, relearning_material
from .clock import ensure_utc, from_utc_iso, to_utc_iso
from .errors import MemorySuppressed, ScopeNotFound, SourceRevisionChanged
from .models import EpistemicStatus, MemoryItem, MemoryStatus, RelearningResult, Scope, new_id
from .storage.repository import _scope_key


def relearn_fact(runtime, memory_id: str, scope: Scope, *, confirmation_event_id: str,
                 approved_by: str, reason: str, idempotency_key: str,
                 source_span: tuple[int, int] | None = None) -> RelearningResult:
    for name, value, limit in (("memory_id", memory_id, 200), ("confirmation_event_id", confirmation_event_id, 200),
                               ("approved_by", approved_by, 200), ("reason", reason, 500),
                               ("idempotency_key", idempotency_key, 200)):
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(f"{name} 必须为非空短字符串（最多 {limit} 字符）")
    if not isinstance(scope, Scope):
        raise ValueError("必须提供可信宿主绑定的完整 scope")
    if source_span is not None:
        if (not isinstance(source_span, (tuple, list)) or len(source_span) != 2
                or any(isinstance(v, bool) or not isinstance(v, int) for v in source_span)
                or not 0 <= source_span[0] < source_span[1]):
            raise ValueError("source_span 必须为有效的 Unicode 码点区间")
        source_span = tuple(source_span)
    db, repos = runtime.db, runtime.db.repositories
    scope_key = _scope_key(scope)
    request_key = repos.forgetting.fingerprint(json.dumps(
        [memory_id, confirmation_event_id, approved_by, reason, source_span],
        ensure_ascii=False, separators=(",", ":"),
    ))

    def existing():
        row = db.connection.execute(
            "SELECT * FROM relearning_grants WHERE scope_key=? AND idempotency_key=?",
            (scope_key, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if row["request_key"] != request_key:
            raise ValueError("同一重新学习幂等键不能对应不同请求")
        item = repos.memories.get(row["memory_id"])
        if (item is None or item.status in (MemoryStatus.DELETED, MemoryStatus.RETRACTED)
                or repos.forgetting.is_suppressed(item)):
            raise MemorySuppressed("原重新学习授权已失效；需要新的用户重申和审批")
        return RelearningResult(row["grant_id"], row["tombstone_id"], row["memory_id"],
                                row["confirmation_event_id"], duplicate=True)

    with db.read_snapshot():
        duplicate = existing()
        if duplicate is not None:
            return duplicate
        old = repos.memories.get(memory_id)
        if old is None or old.scope != scope:
            raise ScopeNotFound("目标记忆不存在或不属于绑定作用域")
        tombstone = repos.forgetting.assertion_tombstone(old)
        if tombstone is None:
            raise ValueError("目标没有事实抑制标记，不需要重新学习")
        barrier = db.connection.execute(
            "SELECT * FROM fact_forgetting_barriers WHERE tombstone_id=?", (tombstone,),
        ).fetchone()
        if barrier is None:
            raise MemorySuppressed("缺少遗忘屏障，请先显式遗忘目标事实")
        event = repos.events.get(confirmation_event_id)
        if (event is None or event.scope != scope or event.speaker != "user"
                or not event.content.strip() or repos.forgetting.is_event_forgotten(confirmation_event_id)):
            raise ValueError("重新学习必须绑定同一完整作用域中未遗忘的真实用户事件")
        if ensure_utc(event.recorded_at) <= from_utc_iso(barrier["forgotten_at"]):
            raise ValueError("确认消息必须在最近一次遗忘之后新写入")
        span = source_span or (0, len(event.content))
        if span[1] > len(event.content):
            raise ValueError("source_span 超出用户原文")
        if db.connection.execute(
            "SELECT 1 FROM relearning_grants WHERE tombstone_id=? AND forgetting_generation=? "
            "AND confirmation_event_id=?", (tombstone, barrier["generation"], confirmation_event_id),
        ).fetchone():
            raise ValueError("该用户确认已用于重新学习，请用原幂等键读取回执")
        generation = repos.retrieval_cache.generation()
        now = runtime.clock.now()
        if ensure_utc(event.recorded_at) > ensure_utc(now):
            raise ValueError("用户事件记录时间晚于当前时钟")
        item = MemoryItem(
            new_id("mem"), old.memory_type, scope, deepcopy(old.body),
            source_event_ids=[event.event_id], source_spans={event.event_id: [list(span)]},
            source_speaker="user", epistemic_status=EpistemicStatus.USER_REPORT,
            modality=old.modality, occurred_start=event.occurred_at,
            occurred_precision=event.occurred_precision, valid_from=event.occurred_at,
            importance=old.importance, recorded_at=now,
        )
        grant_id = new_id("relearn")
        forgetting_generation = barrier["generation"]

    # 编码候选不授予读取/写入权限。授权与记录在后面的同一事务生效。
    text = item.render(400).strip()
    vectors = runtime.vectors.prepare_texts({item.memory_id: text} if text else {})
    with db.transaction():
        duplicate = existing()
        if duplicate is not None:
            return duplicate
        if repos.retrieval_cache.generation() != generation:
            raise SourceRevisionChanged("重新学习计算期间依据或遗忘状态变化，请重新读取后审批")
        now = runtime.clock.now()
        if ensure_utc(now) < ensure_utc(item.recorded_at):
            raise SourceRevisionChanged("重新学习计算期间时钟回拨，请重新读取后审批")
        item.recorded_at = now
        db.connection.execute(
            "INSERT INTO relearning_grants(grant_id,scope_key,idempotency_key,tombstone_id,"
            "forgetting_generation,original_memory_id,memory_id,confirmation_event_id,content_key,"
            "confirmation_key,request_key,approved_by,reason,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (grant_id, scope_key, idempotency_key, tombstone, forgetting_generation, memory_id,
             item.memory_id, event.event_id, repos.forgetting.fingerprint(relearning_material(item)),
             repos.forgetting.fingerprint(confirmation_material(event)), request_key, approved_by, reason, to_utc_iso(now)),
        )
        if repos.forgetting.is_suppressed(item):
            raise MemorySuppressed("新的依据仍与其他被遗忘事实重叠，未解除其他抑制")
        repos.memories.insert(item, extraction_version="host-relearning-v1", dedupe_key=f"relearn:{grant_id}")
        runtime.vectors.index_memory(item, now, prepared=vectors)
        repos.forgetting.bump(scope)
        repos.journal.append(
            "fact.relearned", scope=scope, entity_id=item.memory_id, now=now,
            payload={"grant_id": grant_id, "tombstone_id": tombstone, "confirmation_event_id": event.event_id},
            idempotency_key=f"relearn:{grant_id}",
        )
    return RelearningResult(grant_id, tombstone, item.memory_id, event.event_id)
