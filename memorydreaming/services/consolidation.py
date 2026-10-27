"""可靠整理任务、向量重建与摘要生命周期服务。

抽取/编码运行于事务外，最终提交由租约和来源代次约束。摘要只是可重建投影，
恢复历史正文必须复核来源；摘要撤回不能被后台重建静默解除。
"""

from __future__ import annotations

from typing import Any
from ..consolidation import ConsolidationReport, MemoryExtractor, run_pending_jobs
from ..errors import ScopeNotFound
from ..models import LinkRelation, MemoryStatus, RetrievedItem, Scope
from ..summarization import build_summaries


class ConsolidationService:
    """MemoryRuntime 的业务能力；不直接实例化。"""

    # 队列重试、显式重建与将来的睡眠整理共用这些入口，避免出现两套
    # 会对同一来源产生不同遗忘/租约语义的整理流程。

    def rebuild_vectors(self, scope: Scope) -> int:
        """重建某个角色的向量索引，返回重建条数。

        什么时候需要：
          - 换成真实嵌入模型之后（旧模型的向量不能再用）；
          - 从备份恢复，或者早期版本写的记忆还没有向量；
          - 手动改过记忆正文，怀疑索引漂移。
        """
        return self._vectors.rebuild(scope, self._clock.now())

    def run_consolidation(
        self,
        extractor: MemoryExtractor | None = None,
        limit: int = 10,
    ) -> ConsolidationReport:
        """执行待处理的抽取任务。

        每次调用先回收过期租约。各 worker 须使用独立 runtime/数据库连接；
        文件库会在抽取期间续租，副作用与完成确认在同一事务内提交。
        """
        return run_pending_jobs(
            db=self._db,
            clock=self._clock,
            extractor=extractor,
            vector_index=self._vectors,
            limit=limit,
            tz=self._tz,
            lease_seconds=self.settings.job_lease_seconds,
            retry_base_seconds=self.settings.job_retry_base_seconds,
            retry_max_seconds=self.settings.job_retry_max_seconds,
            max_attempts=self.settings.job_max_attempts,
        )

    def pending_jobs(self) -> int:
        return self._repos.jobs.pending_count()

    def job_stats(self) -> dict[str, int]:
        """队列只读状态，包含等待退避、过期租约和最终失败任务。"""
        return self._repos.jobs.stats(self._clock.now())

    def build_summaries(self, scope: Scope, *, reactivate: bool = False) -> int:
        """立即重建主题摘要，返回卡片数。

        平时不用手动调：extract → dedupe → summarize 会自动串起来。
        这个方法用于「刚导入一批历史数据，想马上有索引」的场景。
        默认保留人工撤回状态；可信宿主可用 reactivate=True 显式重新激活。
        """
        return build_summaries(self._db, scope, self._clock, self._tz, reactivate=reactivate)

    def withdraw_summaries(self, summary_ids: list[str], scope: Scope) -> dict[str, Any]:
        """可信宿主撤回摘要派生物；不会删除其来源记忆。"""
        return self._transition_summaries(summary_ids, scope, "withdrawn")

    def rollback_summaries(self, summary_ids: list[str], scope: Scope) -> dict[str, Any]:
        """标记派生物已撤销，等待显式重新激活；不恢复历史摘要正文。"""
        return self._transition_summaries(summary_ids, scope, "rolled_back")

    def summary_history(self, summary_id: str, scope: Scope, *, limit: int = 100) -> list[dict]:
        from ..temporal import summary_history
        with self._db.transaction(write=False):
            return summary_history(self, summary_id, scope, limit)

    def restore_summary(self, summary_id: str, revision: int, scope: Scope, *, expected_version: int,
                        reason: str, idempotency_key: str) -> dict:
        """显式恢复经过来源复核的历史正文；旧 rollback_summaries 仅撤销卡片。"""
        from ..temporal import restore_summary
        return restore_summary(self, summary_id, revision, scope, expected_version=expected_version,
                               reason=reason, idempotency_key=idempotency_key)

    def _transition_summaries(self, summary_ids: list[str], scope: Scope, status: str) -> dict[str, Any]:
        if not isinstance(summary_ids, list) or not 1 <= len(summary_ids) <= 100:
            raise ValueError("summary_ids 必须包含 1..100 项")
        if any(not isinstance(item, str) or not item for item in summary_ids):
            raise ValueError("summary_id 必须为非空字符串")
        ids = list(dict.fromkeys(summary_ids))
        now = self._clock.now()
        with self._db.transaction():
            summaries = []
            for summary_id in ids:
                summary = self._repos.summaries.get(summary_id)
                if summary is None or not self._scope_matches(summary.scope, scope):
                    raise ScopeNotFound(f"找不到作用域内摘要 {summary_id}")
                summaries.append(summary)
            changed = (self._repos.summaries.mark_withdrawn(ids, now)
                       if status == "withdrawn"
                       else self._repos.summaries.mark_rolled_back(ids, now))
            for summary in summaries:
                if summary.status == status:
                    continue
                self._repos.journal.append(
                    f"summary.{status}", scope=summary.scope, entity_id=summary.summary_id,
                    payload={"status": status}, now=now,
                )
        return {"status": status, "summary_ids": ids, "changed": changed}

    def expand_summary(self, summary_id: str, limit: int = 5) -> list[RetrievedItem]:
        """把一张索引卡回源展开成原始记忆。

        这是「先摘要后展开」的后半段：模型看到摘要后如果觉得相关，
        再用这个方法取具体证据，而不是一上来就塞十条记忆。
        """
        summary = self._repos.summaries.get(summary_id)
        if summary is None:
            raise ScopeNotFound(f"找不到摘要 {summary_id}")
        if summary.status != "active":
            return []

        memories = self._repos.memories.fetch_many(summary.source_memory_ids)
        now = self._clock.now()

        items = [
            RetrievedItem(
                memory=memory,
                # 展开结果不带检索分：它的相关性由摘要决定，不是由这一层重新算
                score=summary.importance,
                channels=["summary_expand"],
                valid_now=memory.is_current(now),
                note="来自主题摘要展开",
            )
            for memory in memories.values()
            if memory.status not in (MemoryStatus.RETRACTED, MemoryStatus.DELETED)
            and not self._repos.forgetting.is_suppressed(memory)
            and self._scope_matches(memory.scope, summary.scope)
            and not (
                memory.status is MemoryStatus.SUPERSEDED
                and self._repos.links.has_relation(memory.memory_id, LinkRelation.PART_OF)
            )
        ]
        items.sort(key=lambda item: (-item.memory.importance, item.memory.recorded_at))
        return items[:limit]
