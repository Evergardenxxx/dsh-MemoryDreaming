"""无正文运维与可移植数据服务。

指标只描述覆盖、队列与缓存状态，不把访问热度解释为真实性。导入、索引重建
和作用域边界继续委托现有仓储/便携协议；运维读取不进入模型上下文。
"""

from __future__ import annotations

import json
from typing import Any
from ..models import ActivityStatus, Scope, new_id
from ..storage.repository import scope_sql, _scope_key
from ..portability import import_snapshot, load_snapshot, preflight_snapshot_file, write_snapshot


class MaintenanceService:
    """MemoryRuntime 的业务能力；不直接实例化。"""

    # 运维结果只有计数、状态与固定告警码，不把记忆正文复制进日志或模型请求。

    def memory_health(self, scope: Scope) -> dict[str, Any]:
        """返回供宿主监控使用的记忆健康摘要。

        健康度只描述覆盖和派生物状态，不把访问次数或模型自评置信度
        解释成事实概率。结果不包含正文，因此可以安全地写入运维日志。
        """
        conn = self._db.connection
        memory_where, memory_params = scope_sql(scope, "m")
        rows = conn.execute(
            "SELECT memory_type, status, COUNT(*) AS n FROM memories m "
            f"WHERE {memory_where} GROUP BY memory_type, status", memory_params,
        ).fetchall()
        memories: dict[str, dict[str, int]] = {}
        for row in rows:
            memories.setdefault(row["memory_type"], {})[row["status"]] = int(row["n"])
        inferred_where, inferred_params = scope_sql(scope, "m")
        inferred = conn.execute(
            "SELECT COUNT(*) AS n FROM memories m WHERE " + inferred_where
            + " AND m.status='active' AND m.epistemic_status='inferred' "
            "AND (m.source_event_ids_json='[]' OR m.source_event_ids_json='')",
            inferred_params,
        ).fetchone()["n"]

        summary_where, summary_params = scope_sql(scope, "s")
        summary_rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM summaries s WHERE " + summary_where
            + " GROUP BY status", summary_params,
        ).fetchall()
        summaries = {row["status"]: int(row["n"]) for row in summary_rows}
        retrieval_cache = self._repos.retrieval_cache.stats(scope, self._clock.now())

        job_where, job_params = scope_sql(scope, "j")
        job_rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM consolidation_jobs j WHERE " + job_where
            + " GROUP BY status", job_params,
        ).fetchall()
        jobs = {row["status"]: int(row["n"]) for row in job_rows}

        activities = self._repos.activities.list_for_scope(
            scope, statuses=(ActivityStatus.ACTIVE, ActivityStatus.PAUSED), limit=1000,
        )
        open_loops = sum(len(activity.open_loops) for activity in activities)
        gaps: list[dict[str, str]] = []
        if summaries.get("stale", 0):
            gaps.append({"code": "STALE_SUMMARIES", "message": "部分摘要等待来源重建"})
        if jobs.get("failed", 0):
            gaps.append({"code": "FAILED_JOBS", "message": "部分后台整理任务达到重试上限"})
        if inferred:
            gaps.append({"code": "INFERRED_WITHOUT_SOURCE", "message": "存在没有事件来源的推断候选"})
        journal = self._repos.journal.verify()
        if not journal["valid"]:
            gaps.append({"code": "JOURNAL_INTEGRITY", "message": "持久审计链完整性校验失败"})
        return {
            "status": "ok",
            "scope": {"user_id": scope.user_id, "character_id": scope.character_id,
                      "relationship_id": scope.relationship_id, "domain": scope.domain},
            "memories": memories,
            "inferred_without_source": int(inferred),
            "summaries": summaries,
            "retrieval_cache": retrieval_cache,
            "jobs": jobs,
            "working": {"active_activities": len(activities), "open_loops": open_loops},
            "journal": journal,
            "gaps": gaps,
        }

    def record_memory_health(self, scope: Scope) -> dict[str, Any]:
        """持久化一份不含记忆正文的健康快照，供趋势/告警系统读取。"""
        captured_at = self._clock.now()
        payload = self.memory_health(scope)
        snapshot_id = new_id("health")
        with self._db.transaction():
            self._db.connection.execute(
                """INSERT INTO health_snapshots (
                    snapshot_id, scope_key, user_id, character_id, relationship_id,
                    activity_id, domain, payload_json, captured_at
                ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (snapshot_id, _scope_key(scope), scope.user_id, scope.character_id,
                 scope.relationship_id, scope.activity_id, scope.domain,
                 json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                 captured_at.isoformat()),
            )
        return {"snapshot_id": snapshot_id, "captured_at": captured_at.isoformat(), "health": payload}

    def memory_health_trend(self, scope: Scope, *, limit: int = 30) -> list[dict[str, Any]]:
        """读取最近健康快照；只返回计数、缺口和审计状态。"""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 365:
            raise ValueError("limit 必须为 1..365")
        visible, params = scope_sql(scope, "h")
        rows = self._db.connection.execute(
            "SELECT snapshot_id, captured_at, payload_json FROM health_snapshots h WHERE "
            + visible + " ORDER BY captured_at DESC LIMIT ?", [*params, limit],
        ).fetchall()
        return [{"snapshot_id": row["snapshot_id"], "captured_at": row["captured_at"],
                 "health": json.loads(row["payload_json"])} for row in rows]

    def operational_trend(self, scope: Scope, *, hours: int = 24) -> list[dict]:
        from ..operations import metric_trend
        return metric_trend(self, scope, hours)

    def check_alerts(self, scope: Scope) -> list[dict]:
        from ..operations import check_alerts
        return check_alerts(self, scope)

    def rebuild_entities(self, scope: Scope) -> int:
        """从当前有效记忆重建作用域实体索引，返回实体提及数。"""
        with self._db.transaction():
            return self._repos.entities.rebuild(scope, self._clock.now())

    def search_entities(self, query: str, scope: Scope, *, limit: int = 20) -> list[dict[str, Any]]:
        """按名称反查实体及其记忆 ID；结果仍需经正常引用读取校验。"""
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query 必须为非空字符串")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit 必须为 1..100")
        return self._repos.entities.search(scope, query, limit=limit)

    def journal_entries(self, scope: Scope | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """读取审计链元数据；默认只返回最近记录，不包含对话正文。"""
        return self._repos.journal.list(scope=scope, limit=limit)

    def verify_journal(self) -> dict[str, Any]:
        """校验从 genesis 到当前 head 的全局哈希链。"""
        return self._repos.journal.verify()

    def export_snapshot(self, path: str, scope: Scope | None = None) -> dict[str, Any]:
        """导出可移植 JSON 快照；不会把快照内容注入模型上下文。"""
        return write_snapshot(self._db, path, scope=scope)

    def preflight_snapshot(self, path: str) -> dict[str, Any]:
        """只读检查快照能否导入当前库，失败时不修改任何数据。"""
        return preflight_snapshot_file(self._db, path)

    def import_snapshot(self, path: str, *, allow_partial: bool = True) -> dict[str, Any]:
        """校验并原子导入快照；失败时整个事务回滚。"""
        return import_snapshot(self._db, load_snapshot(path), allow_partial=allow_partial)
