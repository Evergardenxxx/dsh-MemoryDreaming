"""身份/关系核心、双时间查询与证据组门面。

只有可信宿主调用这里的写入口；模型建议由 proposals 层保存为待审批载荷。
字段补丁、来源版本和依赖撤回共用事务，历史回放仍受当前遗忘账本约束。
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from typing import Any
from ..models import CoreKind, CoreSnapshot, EvidenceRef, Scope


class CoreService:
    """MemoryRuntime 的业务能力；不直接实例化。"""

    # 本层假设调用方可信。面向模型的候选和审批留在 host/proposals，
    # 不能把兼容名称 propose_core_update 当成“模型提交后会自动待审”。

    def propose_core_update(
        self,
        scope: Scope,
        kind: CoreKind | str,
        patch: dict[str, Any],
        change_reason: str,
        expected_version: int | None = None,
        evidence_ids: list[str] | None = None,
        field_evidence_ids: dict[str, list[str]] | None = None,
    ) -> CoreSnapshot:
        """兼容旧版的可信本地直写入口；不会创建待审建议，勿注册为模型工具。"""
        return self.apply_core_update(scope, kind, patch, change_reason, expected_version,
                                      evidence_ids, field_evidence_ids)

    def apply_core_update(
        self, scope: Scope, kind: CoreKind | str, patch: dict[str, Any], change_reason: str,
        expected_version: int | None = None, evidence_ids: list[str] | None = None,
        field_evidence_ids: dict[str, list[str]] | None = None,
        *, effective_from: datetime | None = None,
    ) -> CoreSnapshot:
        """可信本地宿主直接应用一次核心变更；模型建议应走 CoreProposals 审批。

        这里是「补丁合并」语义：patch 里出现的键覆盖旧值，没出现的保持不变。
        版本号由后端计算，调用方只需声明「我是基于哪个版本改的」。

        架构文档的要求：核心变更要可解释。所以 change_reason 是必填项，
        evidence_ids 建议填上支撑这次变更的原始事件。
        """
        core_kind = kind if isinstance(kind, CoreKind) else CoreKind(kind)
        now = self._clock.now()
        from ..clock import ensure_utc
        if effective_from is not None and (not isinstance(effective_from, datetime) or ensure_utc(effective_from) > ensure_utc(now)):
            raise ValueError("effective_from 必须为不晚于当前记录时间的 datetime")
        with self._db.transaction():
            current = self._repos.cores.latest(scope, core_kind)
            merged = dict(current.payload) if current else {}
            merged.update(patch)
            fields = dict(current.field_evidence_ids) if current else {}
            for name in patch:
                fields[name] = list((field_evidence_ids or {}).get(name, evidence_ids or []))
            refs = sorted({ref for values in fields.values() for ref in values})
            result = self._repos.cores.append(
                scope=scope,
                kind=core_kind,
                payload=merged,
                change_reason=change_reason,
                effective_from=effective_from or now,
                now=now,
                evidence_ids=refs,
                expected_version=expected_version,
                field_evidence_ids=fields,
                changed_fields=list(patch),
            )
            # 追记过去的字段变更不能把更晚已经生效的字段覆盖成旧状态。
            from ..temporal import core_at
            projected = core_at(self, scope, core_kind, now, now)
            if projected is not None and (projected.payload != result.payload or projected.field_evidence_ids != result.field_evidence_ids):
                self._db.connection.execute(
                    "UPDATE core_versions SET payload_json=?,field_evidence_json=?,evidence_ids_json=? "
                    "WHERE scope_key=? AND kind=? AND core_version=?",
                    (json.dumps(projected.payload, ensure_ascii=False), json.dumps(projected.field_evidence_ids, ensure_ascii=False),
                     json.dumps(projected.evidence_ids), self._core_key(scope, core_kind), core_kind.value, result.core_version),
                )
                result = replace(result, payload=projected.payload, field_evidence_ids=projected.field_evidence_ids,
                                 evidence_ids=projected.evidence_ids)
            self.refresh_evidence()
            return result

    @staticmethod
    def _core_key(scope, kind):
        from ..assertions import core_scope_key
        return core_scope_key(scope, kind)

    def core_at(self, scope: Scope, kind: CoreKind | str, *, knowledge_at: datetime,
                fact_at: datetime) -> CoreSnapshot | None:
        """按系统获知时间和字段生效时间回放，当前遗忘屏蔽继续生效。"""
        from ..temporal import core_at
        with self._db.transaction(write=False):
            return core_at(self, scope, kind, knowledge_at, fact_at)

    def evidence_ref(self, kind: str, target: str, scope: Scope) -> EvidenceRef:
        """可信宿主获取内容版本锁定的依据；不属于模型写工具。"""
        from ..evidence import evidence_reference
        return evidence_reference(self, kind, target, scope)

    def set_evidence_groups(self, memory_id: str, scope: Scope, groups: list[list[EvidenceRef]],
                            *, expected_version: int, reason: str) -> dict:
        """组内 AND，组间 OR；显式组替代目标的旧扁平来源语义。"""
        from ..evidence import set_groups
        return set_groups(self, memory_id, scope, groups, expected_version=expected_version, reason=reason)

    def refresh_evidence(self) -> list[str]:
        """可信管理入口：撤回已失去全部支持的派生记忆，不自动重新激活。"""
        from ..evidence import invalidate
        with self._db.transaction():
            result = invalidate(self)
            if hasattr(self, "sleep"):
                self.sleep.invalidate_stale()
            for name in ("narrative", "intentions", "companionship", "growth"):
                service = getattr(self, name, None)
                if service and hasattr(service, "invalidate_stale"):
                    service.invalidate_stale()
            return result

    def inspect_evidence(self, memory_id: str, scope: Scope) -> dict:
        from ..evidence import inspect
        return inspect(self, memory_id, scope)
