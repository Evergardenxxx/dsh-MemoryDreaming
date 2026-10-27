"""活动、检查点与跨窗口交接服务。

活动版本用于并发窗口的条件更新；交接是可信宿主提供的工作状态，不升级为
事实或人格核心。服务不创建第二套连接，所有步骤共享门面的同一事务。
"""

from __future__ import annotations

from typing import Any
from ..errors import ScopeNotFound
from ..models import Activity, ActivityStatus, Scope, SessionHandoff, new_id


class ActivityService:
    """MemoryRuntime 的业务能力；不直接实例化。"""

    def register_activity(
        self,
        scope: Scope,
        title: str,
        goal: str = "",
        open_loops: list[str] | None = None,
        active_entities: list[str] | None = None,
    ) -> Activity:
        """登记一段新活动（例如「《海底邮局》的故事」）。

        同一个角色可以有多段活动并行；工作记忆靠 activity_id 隔离，
        这样昨天讲故事讲到一半的状态，不会污染今天学英语的状态。
        """
        now = self._clock.now()
        activity = Activity(
            activity_id=new_id("act"),
            scope=scope,
            title=title,
            goal=goal,
            open_loops=list(open_loops or []),
            active_entities=list(active_entities or []),
            checkpoint={},
            checkpoint_version=1,
            created_at=now,
            updated_at=now,
        )
        with self._db.transaction():
            self._repos.activities.create(activity)
            self._repos.journal.append(
                "activity.created",
                scope=scope,
                entity_id=activity.activity_id,
                payload={"title": title, "goal_digest": self._repos.journal.payload_digest({"goal": goal})},
                now=now,
                idempotency_key=f"activity:{activity.activity_id}:created",
            )
        return activity

    def checkpoint(
        self,
        activity_id: str,
        state: dict[str, Any],
        expected_version: int,
    ) -> int:
        """保存未完成活动的状态，返回新的版本号。

        state 支持的键：
            goal / open_loops / active_entities / checkpoint / status
        其他键会直接报错——把拼写错误当静默忽略，是新手最难查的一类 bug。

        版本号对不上会抛 VersionConflict：说明另一个窗口已经改过这段活动，
        调用方应当重新 bootstrap 再决定怎么合并。
        """
        allowed = {"goal", "open_loops", "active_entities", "checkpoint", "status"}
        unknown = set(state) - allowed
        if unknown:
            raise ValueError(f"未知的检查点字段：{sorted(unknown)}；可用字段：{sorted(allowed)}")

        status = state.get("status")
        if isinstance(status, str):
            status = ActivityStatus(status)

        with self._db.transaction():
            new_version = self._repos.activities.update_checkpoint(
                activity_id=activity_id,
                expected_version=expected_version,
                now=self._clock.now(),
                goal=state.get("goal"),
                open_loops=state.get("open_loops"),
                active_entities=state.get("active_entities"),
                checkpoint=state.get("checkpoint"),
                status=status,
            )
            activity = self._repos.activities.get(activity_id)
            if activity is not None:
                self._repos.journal.append(
                    "activity.checkpoint",
                    scope=activity.scope,
                    entity_id=activity_id,
                    payload={"expected_version": expected_version,
                             "new_version": new_version,
                             "fields": sorted(state)},
                    now=self._clock.now(),
                    idempotency_key=f"checkpoint:{activity_id}:{new_version}",
                )
            return new_version

    def save_handoff(
        self,
        scope: Scope,
        conversation_id: str,
        payload: dict[str, Any],
        *,
        activity_id: str | None = None,
        source_event_id: str | None = None,
        activity_version: int | None = None,
        idempotency_key: str,
    ) -> tuple[SessionHandoff, bool]:
        """持久化上下文压缩前的短交接。

        交接本身不产生记忆事实；它只是下一窗口恢复工作目标和开放循环
        的索引卡。调用方应先用 ``checkpoint`` 做版本化更新，再调用本方法。
        """
        if not isinstance(payload, dict):
            raise ValueError("handoff payload 必须为对象")
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ValueError("conversation_id 必须为非空字符串")
        if activity_id is not None:
            activity = self._repos.activities.get(activity_id)
            expected_scope = Scope(
                scope.user_id, scope.character_id, scope.relationship_id,
                activity_id, scope.domain,
            )
            if activity is None or not self._scope_matches(activity.scope, expected_scope):
                raise ScopeNotFound("找不到当前作用域中的活动")
        handoff_scope = Scope(
            scope.user_id, scope.character_id, scope.relationship_id,
            activity_id, scope.domain,
        )
        now = self._clock.now()
        handoff = SessionHandoff(
            handoff_id=new_id("handoff"),
            scope=handoff_scope,
            conversation_id=conversation_id,
            activity_id=activity_id,
            payload=payload,
            idempotency_key=idempotency_key,
            source_event_id=source_event_id,
            activity_version=activity_version,
            created_at=now,
            updated_at=now,
        )
        stored, duplicate = self._repos.handoffs.upsert(handoff)
        if not duplicate:
            self._repos.journal.append(
                "handoff.created",
                scope=handoff_scope,
                entity_id=stored.handoff_id,
                payload={
                    "conversation_id": conversation_id,
                    "activity_version": activity_version,
                    "payload_digest": self._repos.journal.payload_digest(payload),
                },
                now=now,
                idempotency_key=f"handoff:{stored.handoff_id}:created",
            )
        return stored, duplicate

    def checkpoint_and_save_handoff(
        self,
        scope: Scope,
        activity_id: str,
        state: dict[str, Any],
        expected_version: int,
        conversation_id: str,
        *,
        handoff_extra: dict[str, Any] | None = None,
        source_event_id: str | None = None,
        idempotency_key: str,
    ) -> tuple[SessionHandoff, bool]:
        """原子完成活动检查点和 handoff，避免崩溃留下半个压缩状态。"""
        with self._db.transaction():
            existing = self.get_handoff(
                Scope(scope.user_id, scope.character_id, scope.relationship_id, activity_id, scope.domain),
                idempotency_key,
            )
            if existing is not None:
                return existing, True
            self.checkpoint(activity_id, state, expected_version)
            activity = self._repos.activities.get(activity_id)
            if activity is None:
                raise ScopeNotFound("找不到当前作用域中的活动")
            payload = {
                "title": activity.title,
                "goal": activity.goal,
                "open_loops": activity.open_loops,
                "active_entities": activity.active_entities,
                "checkpoint": activity.checkpoint,
                "status": activity.status.value,
                **(handoff_extra or {}),
            }
            return self.save_handoff(
                scope, conversation_id, payload, activity_id=activity_id,
                source_event_id=source_event_id, activity_version=activity.checkpoint_version,
                idempotency_key=idempotency_key,
            )

    def latest_handoff(self, scope: Scope, *, activity_id: str | None = None) -> SessionHandoff | None:
        """读取同一绑定范围的最新交接，不展开完整历史。"""
        return self._repos.handoffs.latest(scope, activity_id=activity_id)

    def get_handoff(self, scope: Scope, idempotency_key: str) -> SessionHandoff | None:
        """按宿主幂等键读取交接，供重试在修改活动前短路。"""
        return self._repos.handoffs.get_by_key(scope, idempotency_key)
