"""持久运维指标、状态告警和有界预取调度；不向模型注入内容。"""

from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import timedelta
import hashlib
import json
import threading
import time

from .clock import to_utc_iso
from .storage.repository import _scope_key, scope_sql


METRICS = frozenset(("local_hits", "shared_hits", "misses", "stale", "revision_retries", "prefetches",
                    "search", "prefetch_scheduled", "prefetch_completed", "prefetch_cancelled", "prefetch_failed"))


def record_metric(runtime, scope, name, milliseconds=0):
    if name not in METRICS or not 0 <= milliseconds < 86400000:
        raise ValueError("运维指标或耗时无效")
    now = runtime.clock.now()
    bucket = to_utc_iso(now.replace(minute=0, second=0, microsecond=0))
    with runtime.db.transaction():
        runtime.db.connection.execute(
            "INSERT INTO operational_metrics(scope_key,user_id,character_id,relationship_id,activity_id,domain,"
            "bucket,metric,count,total_ms,max_ms) VALUES(?,?,?,?,?,?,?,?,1,?,?) "
            "ON CONFLICT(scope_key,bucket,metric) DO UPDATE SET count=count+1,total_ms=total_ms+excluded.total_ms,"
            "max_ms=MAX(max_ms,excluded.max_ms)",
            (_scope_key(scope), scope.user_id, scope.character_id, scope.relationship_id, scope.activity_id,
             scope.domain, bucket, name, milliseconds, milliseconds),
        )
        runtime.db.connection.execute("DELETE FROM operational_metrics WHERE bucket<?", (to_utc_iso(now-timedelta(days=30)),))


def metric_trend(runtime, scope, hours=24):
    if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= 720:
        raise ValueError("hours 必须为 1..720")
    where, params = scope_sql(scope)
    rows = runtime.db.connection.execute(
        "SELECT bucket,metric,SUM(count) AS count,SUM(total_ms) AS total_ms,MAX(max_ms) AS max_ms "
        "FROM operational_metrics WHERE " + where + " AND bucket>=? GROUP BY bucket,metric ORDER BY bucket,metric",
        [*params, to_utc_iso(runtime.clock.now()-timedelta(hours=hours))],
    ).fetchall()
    return [dict(row) for row in rows]


def check_alerts(runtime, scope):
    health = runtime.memory_health(scope)
    active = {gap["code"]: "warning" for gap in health["gaps"]}
    if "JOURNAL_INTEGRITY" in active:
        active["JOURNAL_INTEGRITY"] = "error"
    totals = {}
    for row in metric_trend(runtime, scope, 1):
        totals[row["metric"]] = totals.get(row["metric"], 0) + row["count"]
    if totals.get("prefetch_failed", 0):
        active["PREFETCH_FAILURE"] = "warning"
    accesses = sum(totals.get(key, 0) for key in ("local_hits", "shared_hits", "misses"))
    if accesses >= 20 and totals.get("stale", 0) > accesses / 2:
        active["HIGH_CACHE_INVALIDATION"] = "info"
    now = to_utc_iso(runtime.clock.now())
    key = _scope_key(scope)
    with runtime.db.transaction():
        for code, severity in active.items():
            runtime.db.connection.execute(
                "INSERT INTO operational_alerts VALUES(?,?,?,'active',?,?,NULL) "
                "ON CONFLICT(scope_key,code) DO UPDATE SET severity=excluded.severity,state='active',"
                "last_seen=excluded.last_seen,resolved_at=NULL", (key, code, severity, now, now),
            )
        for row in runtime.db.connection.execute("SELECT code FROM operational_alerts WHERE scope_key=? AND state='active'", (key,)).fetchall():
            if row[0] not in active:
                runtime.db.connection.execute("UPDATE operational_alerts SET state='resolved',resolved_at=?,last_seen=? WHERE scope_key=? AND code=?",
                                              (now, now, key, row[0]))
    return [dict(row) for row in runtime.db.connection.execute(
        "SELECT code,severity,state,first_seen,last_seen,resolved_at FROM operational_alerts WHERE scope_key=? ORDER BY code", (key,),
    ).fetchall()]


@dataclass(frozen=True)
class _Prefetch:
    owner: str
    binding: object
    event_id: str | None
    query: str
    options: dict
    epoch: int


class PrefetchScheduler:
    """每线程独立连接；待执行查询只在内存中，合并同一会话的待执行项。"""

    def __init__(self, adapter, capacity=32):
        self.adapter = adapter
        self.capacity = capacity
        self._condition = threading.Condition()
        self._queue = OrderedDict()
        self._cancelled = set()
        self._closed = False
        self._thread = None
        self._working = False
        self._running_owner = None
        self.last_error = None

    def submit(self, session, query, options):
        if self.adapter.runtime.db.path is None:
            return {"scheduled": False, "reason": "FILE_DATABASE_REQUIRED"}
        job = _Prefetch(session.binding_ref, session.binding, session.current_event_id, query, dict(options),
                        session.runtime.db.repositories.forgetting.epoch(session.binding.scope))
        with self._condition:
            if self._closed:
                return {"scheduled": False, "reason": "SCHEDULER_CLOSED"}
            if job.owner not in self._queue and len(self._queue) >= self.capacity:
                return {"scheduled": False, "reason": "QUEUE_FULL"}
            self._queue[job.owner] = job
            self._queue.move_to_end(job.owner)
            record_metric(session.runtime, job.binding.scope, "prefetch_scheduled")
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="memory-prefetch", daemon=True)
                self._thread.start()
            self._condition.notify_all()
        return {"scheduled": True, "queued": len(self._queue)}

    def cancel(self, owner):
        with self._condition:
            if self._running_owner == owner:
                self._cancelled.add(owner)
            self._queue.pop(owner, None)
            self._condition.notify_all()

    def drain(self, timeout=5):
        deadline = time.monotonic() + timeout
        with self._condition:
            while (self._queue or self._working) and time.monotonic() < deadline:
                self._condition.wait(max(0, deadline-time.monotonic()))
            return not self._queue and not self._working

    def close(self, timeout=5):
        with self._condition:
            self._closed = True
            self._queue.clear()
            self._condition.notify_all()
        if self._thread:
            self._thread.join(timeout)

    def _run(self):
        from .host import HostAdapter, MemorySession
        from .runtime import MemoryRuntime
        runtime = None
        try:
            settings = replace(self.adapter.runtime.settings, auto_consolidation=False)
            runtime = MemoryRuntime(settings, clock=self.adapter.clock, embedder=self.adapter.runtime.vectors._embedder)
            adapter = HostAdapter(runtime, clock=self.adapter.clock,
                                  retrieval_cache_ttl_seconds=self.adapter.retrieval_cache_ttl_seconds)
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self._closed or self._queue)
                    if self._closed:
                        return
                    _, job = self._queue.popitem(last=False)
                    self._working = True
                    self._running_owner = job.owner
                    cancelled = job.owner in self._cancelled
                try:
                    stale = runtime.db.repositories.forgetting.epoch(job.binding.scope) != job.epoch
                    event = runtime.db.repositories.events.get(job.event_id) if job.event_id else None
                    if job.event_id and (event is None or runtime.db.repositories.forgetting.is_event_forgotten(job.event_id)):
                        stale = True
                    if cancelled or stale:
                        record_metric(runtime, job.binding.scope, "prefetch_cancelled")
                        continue
                    session = MemorySession(adapter, job.binding)
                    session.current_event_id = job.event_id
                    try:
                        result = session.prefetch(job.query, **job.options)
                        record_metric(runtime, job.binding.scope, "prefetch_completed" if result["cached"] else "prefetch_failed")
                    finally:
                        session.close()
                except Exception:
                    record_metric(runtime, job.binding.scope, "prefetch_failed")
                finally:
                    with self._condition:
                        self._working = False
                        self._running_owner = None
                        self._cancelled.discard(job.owner)
                        self._condition.notify_all()
        except Exception:
            self.last_error = "PREFETCH_WORKER_FAILED"
        finally:
            if runtime:
                runtime.close()
            with self._condition:
                self._working = False
                self._queue.clear()
                self._condition.notify_all()
