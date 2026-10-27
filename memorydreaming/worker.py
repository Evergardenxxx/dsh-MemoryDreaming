"""文件数据库上的任务心跳。每个线程独占连接，结果提交仍以租约校验为准。"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from .clock import Clock, load_timezone
from .errors import JobLeaseLost
from .storage.database import Database
from .storage.repository import JobRecord

if TYPE_CHECKING:
    from .consolidation import ConsolidationReport, MemoryExtractor
    from .retrieval.embedding import Embedder

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


class ConsolidationWorker:
    """可唤醒的后台整理循环。

    它刻意不复用宿主线程的 SQLite 连接：sqlite3 默认连接绑定创建线程，
    而且抽取器可能执行较慢的模型调用。worker 自己打开文件连接，借助已有的
    job lease/heartbeat 与手工 ``run_consolidation`` 安全并发。

    ``:memory:`` 数据库没有跨连接共享语义，因此运行时不会为它启动后台 worker。
    这种情况下调用方仍可显式执行 ``run_consolidation``。
    """

    def __init__(
        self,
        db_path: Path,
        clock: Clock,
        extractor: "MemoryExtractor",
        *,
        embedder: "Embedder | None" = None,
        timezone: str = "UTC",
        interval_seconds: float = 1.0,
        batch_size: int = 4,
        lease_seconds: float = 120,
        retry_base_seconds: float = 5,
        retry_max_seconds: float = 300,
        max_attempts: int = 3,
    ) -> None:
        self._path = Path(db_path)
        self._clock = clock
        self._extractor = extractor
        self._embedder = embedder
        self._timezone = timezone
        self._interval = float(interval_seconds)
        self._batch_size = int(batch_size)
        self._lease_seconds = lease_seconds
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds
        self._max_attempts = max_attempts
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_report: "ConsolidationReport | None" = None
        self._last_error: str | None = None

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def last_report(self) -> "ConsolidationReport | None":
        with self._lock:
            return self._last_report

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._wake_event.set()  # 启动时也清理进程启动前遗留的任务
        self._thread = threading.Thread(
            target=self._run, name="memory-consolidation", daemon=True
        )
        self._thread.start()

    def wake(self) -> None:
        """通知 worker 立即检查队列；没有 worker 时是安全空操作。"""
        self._wake_event.set()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        if thread is None or not thread.is_alive():
            self._thread = None

    def _run(self) -> None:
        # 延迟导入避免 worker ↔ consolidation 的循环依赖。
        from .consolidation import run_pending_jobs
        from .retrieval.vector import VectorIndex
        from .storage.database import Database

        db = None
        try:
            db = Database.open(self._path, initialize=False)
            vectors = VectorIndex(db, self._embedder)
            while not self._stop_event.is_set():
                self._wake_event.wait(self._interval)
                self._wake_event.clear()
                if self._stop_event.is_set():
                    break

                # 一次唤醒尽量跑完当前批次，避免每条消息都启动一个线程；
                # limit 仍然受控，长队列不会阻塞关闭和其他进程。
                while not self._stop_event.is_set():
                    report = run_pending_jobs(
                        db=db,
                        clock=self._clock,
                        extractor=self._extractor,
                        vector_index=vectors,
                        limit=self._batch_size,
                        tz=load_timezone(self._timezone),
                        lease_seconds=self._lease_seconds,
                        retry_base_seconds=self._retry_base_seconds,
                        retry_max_seconds=self._retry_max_seconds,
                        max_attempts=self._max_attempts,
                    )
                    with self._lock:
                        self._last_report = report
                        self._last_error = None
                    if report.claimed == 0:
                        break
                    # 失败任务有退避时间；继续循环只会空转，等下一次唤醒。
                    if report.failed or report.lease_lost or report.stale_results or report.deferred:
                        break
        except Exception as exc:  # 后台故障不能杀死宿主进程
            logger.exception("记忆后台整理器停止")
            with self._lock:
                self._last_error = str(exc)
        finally:
            if db is not None:
                db.close()
