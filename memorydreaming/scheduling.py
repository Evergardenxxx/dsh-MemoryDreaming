"""同一 SQLite 库中的 DeepSeek 请求闸门；网络与等待均不持写锁。"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import logging
import secrets
import sqlite3
import threading
from typing import Callable, TypeVar
from urllib.parse import urlsplit, urlunsplit

from .clock import Clock, ensure_utc, from_utc_iso, to_utc_iso
from .errors import StorageError
from .llm_extractor import DeepSeekClient, LLMExtractionError
from .storage.database import Database

logger = logging.getLogger(__name__)
T = TypeVar("T")


class ExtractionDeferred(Exception):
    """没有请求名额时主动重排；attempted 表示此前已发过格式失败的请求。"""

    def __init__(self, retry_at: datetime, code: str, *, attempted: bool = False):
        super().__init__("DeepSeek 请求等待共享调度")
        self.retry_at, self.code, self.attempted = retry_at, code, attempted


@dataclass(frozen=True)
class RequestPermit:
    token: str
    provider_key: str


class DeepSeekRequestScheduler:
    """原子领取请求名额、持久共享冷却、心跳续租、退出释放。

    作用域是端点与凭据，不包含 model、event 或用户正文。不同模型共用同一
    账户闸门；不同数据库不共享配额。该类仅供可信宿主和队列使用。
    """

    def __init__(self, db: Database, clock: Clock, client: DeepSeekClient):
        self.db, self.clock, self.policy = db, clock, client.scheduling_policy
        parts = urlsplit(client.base_url)
        endpoint = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query, ""))
        self.provider_key = db.repositories.forgetting.fingerprint(json.dumps(
            ["deepseek-request-v1", endpoint, client.api_key], separators=(",", ":")))

    def _outside_transaction(self):
        if self.db.connection.in_transaction:
            raise StorageError("DeepSeek 请求调度必须在外部事务之外调用")

    def _configure(self, now):
        self.db.connection.execute(
            "INSERT INTO provider_limits(provider_key,max_concurrent,min_interval_seconds,not_before,next_start_at,updated_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(provider_key) DO UPDATE SET "
            "max_concurrent=MIN(max_concurrent,excluded.max_concurrent), "
            "min_interval_seconds=MAX(min_interval_seconds,excluded.min_interval_seconds),updated_at=excluded.updated_at",
            (self.provider_key, self.policy.max_concurrent, self.policy.min_interval_seconds,
             to_utc_iso(now), to_utc_iso(now), to_utc_iso(now)),
        )

    def acquire(self, *, attempted=False) -> RequestPermit:
        self._outside_transaction()
        with self.db.transaction():
            now = self.clock.now()
            self._configure(now)
            self.db.connection.execute("DELETE FROM provider_requests WHERE provider_key=? AND lease_until<=?",
                                       (self.provider_key, to_utc_iso(now)))
            row = self.db.connection.execute("SELECT * FROM provider_limits WHERE provider_key=?",
                                             (self.provider_key,)).fetchone()
            active = self.db.connection.execute(
                "SELECT COUNT(*),MIN(lease_until) FROM provider_requests WHERE provider_key=?", (self.provider_key,)
            ).fetchone()
            retry_at, code = max(from_utc_iso(row["not_before"]), from_utc_iso(row["next_start_at"])), "PROVIDER_COOLDOWN"
            if active[0] >= row["max_concurrent"]:
                retry_at = max(retry_at, min(from_utc_iso(active[1]), ensure_utc(now) + timedelta(seconds=1)))
                code = "PROVIDER_BUSY"
            if retry_at > ensure_utc(now):
                # 抛出异常会回滚配置/过期清理；先提交，再把延后信息交给队列。
                deferred = ExtractionDeferred(retry_at, code, attempted=attempted)
            else:
                deferred = None
                permit = RequestPermit(secrets.token_urlsafe(24), self.provider_key)
                self.db.connection.execute(
                    "INSERT INTO provider_requests(request_token,provider_key,started_at,lease_until) VALUES (?,?,?,?)",
                    (permit.token, permit.provider_key, to_utc_iso(now),
                     to_utc_iso(now + timedelta(seconds=self.policy.lease_seconds))),
                )
                self.db.connection.execute(
                    "UPDATE provider_limits SET next_start_at=?,updated_at=? WHERE provider_key=?",
                    (to_utc_iso(now + timedelta(seconds=row["min_interval_seconds"])), to_utc_iso(now), self.provider_key),
                )
        if deferred is not None:
            raise deferred
        return permit

    def _owned(self, permit):
        return self.db.connection.execute(
            "SELECT 1 FROM provider_requests WHERE request_token=? AND provider_key=? AND lease_until>?",
            (permit.token, self.provider_key, to_utc_iso(self.clock.now())),
        ).fetchone() is not None

    def _finish(self, permit, error):
        with self.db.transaction():
            owned = self._owned(permit)
            now = self.clock.now()
            if isinstance(error, LLMExtractionError) and error.retryable and (
                error.code == "RATE_LIMITED" or error.retry_after_seconds is not None
            ):
                # 即便请求租约已失效，真实供应商的限流信息仍可以单调收紧闸门。
                delay = max(self.policy.cooldown_seconds, error.retry_after_seconds or 0)
                self.db.connection.execute(
                    "UPDATE provider_limits SET not_before=MAX(not_before,?),updated_at=? WHERE provider_key=?",
                    (to_utc_iso(now + timedelta(seconds=delay)), to_utc_iso(now), self.provider_key),
                )
            self.db.connection.execute(
                "DELETE FROM provider_requests WHERE request_token=? AND provider_key=?", (permit.token, self.provider_key))
        if error is None and not owned:
            raise LLMExtractionError("DeepSeek 请求租约已失效，结果已丢弃", code="REQUEST_LEASE_LOST")

    def call(self, invoke: Callable[[], T], *, attempted=False) -> T:
        permit = self.acquire(attempted=attempted)
        error = None
        try:
            with self._heartbeat(permit):
                return invoke()
        except BaseException as exc:
            error = exc
            raise
        finally:
            self._finish(permit, error)

    @contextmanager
    def _heartbeat(self, permit):
        stop = threading.Event()
        thread = None
        if self.db.path is not None:
            thread = threading.Thread(target=self._renew_loop, args=(permit, stop),
                                      name="deepseek-request-heartbeat", daemon=True)
            thread.start()
        try:
            yield
        finally:
            stop.set()
            if thread is not None:
                thread.join()

    def _renew_loop(self, permit, stop):
        db = None
        try:
            db = Database.open(self.db.path, initialize=False)
            db.connection.execute("PRAGMA busy_timeout=100")
            while not stop.wait(min(30, self.policy.lease_seconds / 3)):
                try:
                    with db.transaction():
                        now = self.clock.now()
                        cursor = db.connection.execute(
                            "UPDATE provider_requests SET lease_until=MAX(lease_until,?) "
                            "WHERE request_token=? AND provider_key=? AND lease_until>?",
                            (to_utc_iso(now + timedelta(seconds=self.policy.lease_seconds)),
                             permit.token, permit.provider_key, to_utc_iso(now)),
                        )
                        if cursor.rowcount != 1:
                            return
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                        raise
        except Exception:
            logger.exception("DeepSeek 请求心跳停止；调用完成后复核租约")
        finally:
            if db is not None:
                db.close()

    def status(self):
        """可信运维快照，不含凭据、端点、事件或正文。"""
        self._outside_transaction()
        with self.db.read_snapshot():
            row = self.db.connection.execute("SELECT * FROM provider_limits WHERE provider_key=?",
                                             (self.provider_key,)).fetchone()
            active = self.db.connection.execute(
                "SELECT COUNT(*) FROM provider_requests WHERE provider_key=? AND lease_until>?",
                (self.provider_key, to_utc_iso(self.clock.now())),
            ).fetchone()[0]
        return {"active_requests": active, "max_concurrent": row["max_concurrent"] if row else self.policy.max_concurrent,
                "min_interval_seconds": row["min_interval_seconds"] if row else self.policy.min_interval_seconds,
                "not_before": row["not_before"] if row else None, "next_start_at": row["next_start_at"] if row else None}

    def reconfigure(self):
        """无在途请求时显式放宽到当前配置；保留服务端冷却和已预订的间隔。"""
        self._outside_transaction()
        with self.db.transaction():
            now = self.clock.now()
            if self.db.connection.execute(
                "SELECT 1 FROM provider_requests WHERE provider_key=? AND lease_until>?",
                (self.provider_key, to_utc_iso(now)),
            ).fetchone():
                raise StorageError("仍有在途 DeepSeek 请求，不能重新配置配额")
            self._configure(now)
            self.db.connection.execute(
                "UPDATE provider_limits SET max_concurrent=?,min_interval_seconds=? WHERE provider_key=?",
                (self.policy.max_concurrent, self.policy.min_interval_seconds, self.provider_key),
            )
