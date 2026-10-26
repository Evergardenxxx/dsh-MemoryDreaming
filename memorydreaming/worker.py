"""文件数据库上的任务心跳。每个线程独占连接，结果提交仍以租约校验为准。"""

from __future__ import annotations

import logging
import sqlite3
import threading

from .clock import Clock
from .errors import JobLeaseLost
from .storage.database import Database
from .storage.repository import JobRecord

logger = logging.getLogger(__name__)


class JobHeartbeat:
    """模型抽取在事务外执行时续租；内存数据库仅使用同步的最终校验。"""

    def __init__(self, db: Database, job: JobRecord, clock: Clock, lease_seconds: float) -> None:
        self._path = db.path
        self._job = job
        self._clock = clock
        self._lease_seconds = lease_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> JobHeartbeat:
        if self._path is not None:
            self._thread = threading.Thread(target=self._run, name="memory-job-heartbeat", daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def _run(self) -> None:
        db: Database | None = None
        try:
            db = Database.open(self._path, initialize=False)
            # 心跳不长时间占用线程；写事务繁忙时稍后重试，不能绕过最终校验。
            db.connection.execute("PRAGMA busy_timeout = 100")
            interval = min(30.0, self._lease_seconds / 3)
            while not self._stop.wait(interval):
                try:
                    with db.transaction():
                        db.repositories.jobs.renew(
                            self._job.job_id, self._job.claim_token, self._clock.now(), self._lease_seconds,
                        )
                except JobLeaseLost:
                    return
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                        raise
        except Exception:
            logger.exception("任务心跳停止；结果提交时将再次检查租约")
        finally:
            if db is not None:
                db.close()
