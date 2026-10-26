"""数据库连接、事务与初始化。

三个和 SQLite 打交道时容易踩的坑，这个文件替你处理掉：

1. **事务边界**。本项目要求「消息事件 + 活动检查点 + 整理任务」写在同一个事务里，
   否则进程被杀时会出现「用户说的话没记下来，但检查点已经往前走了」。
   这里关掉 sqlite3 的隐式事务（isolation_level=None），改用手动 BEGIN，
   并用 `transaction()` 上下文管理器统一收口；嵌套调用会自动降级为 SAVEPOINT。

2. **中文全文检索**。建表之后还要准备 FTS5 索引，具体策略在 retrieval/keyword.py
   里解释（那里会按运行环境选择 trigram 或降级为 LIKE）。

3. **结构版本**。schema_meta 里记录 schema_version。将来改表结构时，
   可以据此决定要不要跑迁移脚本，而不是靠猜。
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ..errors import StorageError
from ..clock import utcnow
from ..assertions import core_scope_key
from ..models import CoreKind, Scope
from ..retrieval.keyword import KeywordIndex
from .repository import Repositories, _scope_key, build_repositories

# 当前数据库结构版本。改动 schema.sql 时必须加一，并补上迁移逻辑。
#
# 版本 2：新增 memory_vectors（向量索引）。它是纯新增表，老库打开时会由
#         CREATE TABLE IF NOT EXISTS 自动补上，不需要数据迁移。
# 版本 3：新增 summaries（主题摘要）。同样是纯新增表，无需迁移。
# 版本 4：事件幂等按作用域隔离，摘要/任务持久保存完整作用域。
# 版本 5：任务租约、提交凭证和延迟重试。
# 版本 6：断言抑制、来源片段、内容代次及作用域独立的核心字段来源。
# 版本 7：宿主实际投递记录，纯新增表。
# 版本 8：核心候选与宿主审批回执，纯新增表。
# 版本 9：事件级遗忘账本，保留事件行但撤销读取、抽取和来源贡献。
# 版本 10：上下文压缩/窗口结束的持久 handoff 交接记录。
# 版本 11：摘要保存来源指纹和生成算法版本。
# 版本 12：追加式 durable journal/hash-chain 审计链。
# 版本 13：记忆系统时间修订日志与作用域物理清除账本。
# 版本 14：不含正文的健康趋势快照。
# 版本 15：一般事实/遗忘/检查点提议。
# 版本 16：可重建实体索引。
# 版本 17：跨进程检索缓存（只存候选 ID 与版本指纹）。
SCHEMA_VERSION = "17"

_SCHEMA_FILE = Path(__file__).with_name("schema.sql")


class Database:
    """一个 SQLite 连接 + 一组仓储。整个进程通常只需要一个实例。"""

    def __init__(self, conn: sqlite3.Connection, path: Path | None) -> None:
        self._conn = conn
        self._path = path
        self._depth = 0  # 事务嵌套深度，用于决定 BEGIN 还是 SAVEPOINT
        self._repos: Repositories | None = None

    # -- 创建与关闭 -------------------------------------------------------

    @classmethod
    def open(
        cls,
        db_path: str | Path = ":memory:",
        *,
        initialize: bool = True,
    ) -> "Database":
        """打开（或创建）数据库。

        参数:
            db_path: 文件路径，或 ":memory:"（测试用，进程结束即消失）。
            initialize: 是否立刻建表。测试里想手工控制时可以关掉。
        """
        path: Path | None
        if str(db_path) == ":memory:":
            path = None
            target = ":memory:"
        else:
            path = Path(db_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            target = str(path)

        # isolation_level=None：不让 sqlite3 自己开事务，全部由 transaction() 控制
        conn = sqlite3.connect(target, isolation_level=None)
        conn.row_factory = sqlite3.Row  # 让查询结果可以按列名取值

        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")  # 另一个进程在写时等 5 秒而不是立刻报错
        if path is not None:
            # WAL 模式让「读」和「写」互不阻塞，本地单机场景更稳
            conn.execute("PRAGMA journal_mode = WAL")

        database = cls(conn, path)
        if initialize:
            try:
                database.initialize()
            except Exception:
                conn.close()
                raise
        return database

    @property
    def connection(self) -> sqlite3.Connection:
        """裸连接。只在写迁移脚本或调试时用，业务代码请走仓储。"""
        return self._conn

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def repositories(self) -> Repositories:
        """仓储集合，惰性创建一次。"""
        if self._repos is None:
            self._repos = build_repositories(self._conn)
        return self._repos

    def close(self) -> None:
        self._conn.close()

    # -- 事务 -------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """开一个事务。用法：

            with db.transaction():
                events.insert(...)
                activities.update_checkpoint(...)

        嵌套时内层用 SAVEPOINT，出错只回滚内层，外层仍然可以继续或整体回滚。
        这个设计是为了让「runtime 开大事务、内部小方法也敢开事务」而不互相破坏。
        """
        outermost = self._depth == 0
        if outermost:
            self._conn.execute("BEGIN IMMEDIATE")
        else:
            self._conn.execute(f"SAVEPOINT sp{self._depth}")

        self._depth += 1
        try:
            yield self._conn
        except Exception:
            self._depth -= 1
            if outermost:
                self._conn.execute("ROLLBACK")
            else:
                self._conn.execute(f"ROLLBACK TO sp{self._depth}")
                self._conn.execute(f"RELEASE sp{self._depth}")
            raise
        else:
            self._depth -= 1
            if outermost:
                self._conn.execute("COMMIT")
            else:
                self._conn.execute(f"RELEASE sp{self._depth}")

    # -- 初始化 -----------------------------------------------------------

    def initialize(self) -> None:
        """验证版本、原子迁移、建表；拒绝较新数据库，不能静默降版本。"""
        if not _SCHEMA_FILE.exists():
            raise StorageError(f"找不到建表脚本：{_SCHEMA_FILE}")

        sql = _SCHEMA_FILE.read_text(encoding="utf-8")
        has_meta = self._table_exists("schema_meta")
        raw_version = self.meta_get("schema_version") if has_meta else None
        try:
            version = int(raw_version or "0")
        except ValueError as exc:
            raise StorageError(f"无法识别数据库结构版本：{raw_version!r}") from exc
        if version > int(SCHEMA_VERSION):
            raise StorageError(
                f"数据库结构版本 {version} 高于程序支持的 {SCHEMA_VERSION}，请升级程序"
            )
        try:
            with self.transaction():
                if version < 4:
                    self._migrate_scope_schema(sql)
                if version < 5 and self._table_exists("consolidation_jobs"):
                    self._migrate_job_leases()
                if version < 6:
                    self._migrate_forgetting(sql)
                if version < 11:
                    self._migrate_summary_metadata()
                for statement in self._statements(sql):
                    self._conn.execute(statement)
                if version < 13:
                    self._migrate_memory_revisions()
                if self.meta_get("forgetting_hmac_key") is None:
                    self.meta_set("forgetting_hmac_key", secrets.token_hex(32))
                if version < 6:
                    # 旧版已删除的事实也不能通过换抽取器版本复活。
                    for row in self._conn.execute("SELECT memory_id FROM memories WHERE status = 'deleted'").fetchall():
                        item = self.repositories.memories.get(row["memory_id"])
                        self.repositories.forgetting.suppress(item, item.recorded_at)
                    self.repositories.forgetting.redact_cores(utcnow())
                self.meta_set("schema_version", SCHEMA_VERSION)
        except sqlite3.Error as exc:  # 通常是磁盘不可写或文件损坏
            raise StorageError(f"初始化数据库失败：{exc}") from exc

        # 关键词索引依赖 memories 表存在，必须放在建表之后
        KeywordIndex(self._conn).ensure_index()

    def _migrate_memory_revisions(self) -> None:
        """为旧库当前投影补一条 revision=1，避免历史查询误报完整历史。"""
        rows = self._conn.execute("SELECT * FROM memories").fetchall()
        for row in rows:
            snapshot = {
                "memory_id": row["memory_id"], "memory_type": row["memory_type"],
                "schema_version": row["schema_version"], "body": json.loads(row["body_json"]),
                "source_event_ids": json.loads(row["source_event_ids_json"] or "[]"),
                "source_spans": json.loads(row["source_spans_json"] or "{}"),
                "source_speaker": row["source_speaker"], "epistemic_status": row["epistemic_status"],
                "modality": row["modality"], "occurred_start": row["occurred_start"],
                "occurred_end": row["occurred_end"], "occurred_precision": row["occurred_precision"],
                "valid_from": row["valid_from"], "valid_to": row["valid_to"],
                "supersedes_id": row["supersedes_id"], "status": row["status"],
                "importance": row["importance"], "retrieval_strength": row["retrieval_strength"],
                "last_used_at": row["last_used_at"], "access_count": row["access_count"],
                "recorded_at": row["recorded_at"], "updated_at": row["updated_at"],
            }
            self._conn.execute(
                """
                INSERT OR IGNORE INTO memory_revisions (
                    revision_id, memory_id, revision, user_id, character_id, relationship_id,
                    activity_id, domain, memory_type, status, valid_from, valid_to,
                    snapshot_json, system_from, system_to, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"{row['memory_id']}:r1", row["memory_id"], 1, row["user_id"],
                    row["character_id"], row["relationship_id"], row["activity_id"], row["domain"],
                    row["memory_type"], row["status"], row["valid_from"], row["valid_to"],
                    json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    row["recorded_at"], None, row["recorded_at"],
                ),
            )

    def _table_exists(self, name: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone() is not None

    def _migrate_job_leases(self) -> None:
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(consolidation_jobs)")}
        additions = {
            "max_attempts": "INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts > 0)",
            "available_at": "TEXT NOT NULL DEFAULT '1970-01-01T00:00:00.000000+00:00'",
            "claim_token": "TEXT",
            "lease_until": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                self._conn.execute(f"ALTER TABLE consolidation_jobs ADD COLUMN {name} {definition}")
        self._conn.execute(
            "UPDATE consolidation_jobs SET available_at = created_at "
            "WHERE available_at = '1970-01-01T00:00:00.000000+00:00'"
        )
        # 旧 running 没有所有权凭证；下一次领取时按失联任务回收。

    def _migrate_forgetting(self, sql: str) -> None:
        if self._table_exists("memories"):
            columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(memories)")}
            if "source_spans_json" not in columns:
                self._conn.execute("ALTER TABLE memories ADD COLUMN source_spans_json TEXT NOT NULL DEFAULT '{}'")
        if not self._table_exists("core_versions"):
            return
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(core_versions)")}
        if "scope_key" in columns:
            return
        replacement = self._create_migration_table(sql, "core_versions")
        rows = self._conn.execute("SELECT * FROM core_versions").fetchall()
        for row in rows:
            kind = CoreKind(row["kind"])
            scope = Scope(row["user_id"], row["character_id"], row["relationship_id"])
            payload = json.loads(row["payload_json"])
            evidence = json.loads(row["evidence_ids_json"])
            fields = {key: evidence for key in payload}
            self._conn.execute(
                f"INSERT INTO {replacement} (scope_key,user_id,character_id,kind,core_version,relationship_id,"
                "domain,payload_json,evidence_ids_json,field_evidence_json,change_reason,status,effective_from,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (core_scope_key(scope, kind), scope.user_id, scope.character_id, kind.value,
                 row["core_version"], scope.relationship_id if kind is CoreKind.RELATIONSHIP else None,
                 "real", row["payload_json"], row["evidence_ids_json"], json.dumps(fields, ensure_ascii=False),
                 row["change_reason"], row["status"], row["effective_from"], row["created_at"]),
            )
        self._conn.execute("DROP TABLE core_versions")
        self._conn.execute(f"ALTER TABLE {replacement} RENAME TO core_versions")

    def _migrate_summary_metadata(self) -> None:
        if not self._table_exists("summaries"):
            return
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(summaries)")}
        if "source_digest" not in columns:
            self._conn.execute("ALTER TABLE summaries ADD COLUMN source_digest TEXT NOT NULL DEFAULT ''")
        if "algorithm_version" not in columns:
            self._conn.execute(
                "ALTER TABLE summaries ADD COLUMN algorithm_version TEXT NOT NULL DEFAULT 'extractive-v1'"
            )

    @staticmethod
    def _statements(sql: str) -> list[str]:
        """逐条执行 DDL，避免 executescript 在迁移中隐式提交。"""
        statements: list[str] = []
        pending = ""
        for line in sql.splitlines(keepends=True):
            pending += line
            if sqlite3.complete_statement(pending):
                statements.append(pending)
                pending = ""
        return statements

    def _create_migration_table(self, sql: str, name: str) -> str:
        replacement = name + "_scope_v4"
        pattern = rf"(CREATE TABLE IF NOT EXISTS\s+){name}\b"
        for statement in self._statements(sql):
            if re.search(pattern, statement):
                self._conn.execute(re.sub(pattern, rf"\g<1>{replacement}", statement, count=1))
                return replacement
        raise StorageError(f"建表脚本缺少 {name}")

    def _migrate_scope_schema(self, sql: str) -> None:
        """保留原记录与 ID；无法可靠还原作用域的旧摘要先失效。"""
        if self._table_exists("events"):
            # 移除原 idempotency_key 的全局 UNIQUE autoindex，必须重建表。
            temporary = self._create_migration_table(sql, "events")
            columns = [row["name"] for row in self._conn.execute("PRAGMA table_info(events)")]
            names = ", ".join(columns)
            self._conn.execute(f"INSERT INTO {temporary} ({names}) SELECT {names} FROM events")
            self._conn.execute("DROP TABLE events")
            self._conn.execute(f"ALTER TABLE {temporary} RENAME TO events")

        if self._table_exists("summaries"):
            rows = self._conn.execute("SELECT * FROM summaries").fetchall()
            temporary = self._create_migration_table(sql, "summaries")
            new_columns = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({temporary})")}
            for row in rows:
                data = dict(row)
                data.setdefault("relationship_id", None)
                data.setdefault("domain", "real")
                data["scope_key"] = _scope_key(Scope(
                    user_id=data["user_id"], character_id=data["character_id"],
                    relationship_id=data["relationship_id"], activity_id=data.get("activity_id"),
                    domain=data["domain"],
                ))
                # 旧版摘要只记录 user/character，可能已混入虚构或不同关系内容。
                data["status"] = "stale"
                columns = [name for name in data if name in new_columns]
                self._conn.execute(
                    f"INSERT INTO {temporary} ({', '.join(columns)}) "
                    f"VALUES ({', '.join('?' for _ in columns)})",
                    [data[name] for name in columns],
                )
            self._conn.execute("DROP TABLE summaries")
            self._conn.execute(f"ALTER TABLE {temporary} RENAME TO summaries")

        if self._table_exists("consolidation_jobs"):
            columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(consolidation_jobs)")}
            if "relationship_id" not in columns:
                self._conn.execute("ALTER TABLE consolidation_jobs ADD COLUMN relationship_id TEXT")
            if "domain" not in columns:
                self._conn.execute(
                    "ALTER TABLE consolidation_jobs ADD COLUMN domain TEXT NOT NULL DEFAULT 'real'"
                )
            # 抽取任务可从原始事件可靠还原范围；其余旧任务从活动还原。
            jobs = self._conn.execute("SELECT * FROM consolidation_jobs").fetchall()
            for job in jobs:
                source = None
                if job["job_type"] == "extract" and self._table_exists("events"):
                    try:
                        payload = json.loads(job["payload_json"])
                    except (json.JSONDecodeError, TypeError):
                        payload = {}
                    if isinstance(payload, dict):
                        source = self._conn.execute(
                            "SELECT * FROM events WHERE event_id = ? AND user_id = ? AND character_id = ?",
                            (payload.get("event_id"), job["user_id"], job["character_id"]),
                        ).fetchone()
                elif job["activity_id"] and self._table_exists("activities"):
                    source = self._conn.execute(
                        "SELECT * FROM activities WHERE activity_id = ? AND user_id = ? AND character_id = ?",
                        (job["activity_id"], job["user_id"], job["character_id"]),
                    ).fetchone()
                if source is not None:
                    self._conn.execute(
                        "UPDATE consolidation_jobs SET relationship_id = ?, domain = ?, activity_id = ? "
                        "WHERE job_id = ?",
                        (source["relationship_id"], source["domain"], source["activity_id"], job["job_id"]),
                    )

    # -- 元信息 -----------------------------------------------------------

    def meta_get(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM schema_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def meta_set(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
