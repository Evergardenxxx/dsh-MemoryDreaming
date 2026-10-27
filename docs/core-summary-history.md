# 核心/摘要历史与批量审批（v0.25，schema 24）

本工作包已完成三个功能：核心字段双时间回放、摘要历史正文恢复、已有单项提议的原子批量审批。它们通过可信宿主 SDK 使用，不新增模型写工具；DeepSeek Harness 的原生 Cordis/JSONL 接入方式保持不变。

## 核心双时间

```python
# 27 日收到确认：称呼从 25 日起改变。记录时间来自引擎时钟。
memory.apply_core_update(
    scope, "relationship", {"称呼": "老爸"}, "用户确认从 25 日起更换称呼",
    expected_version=1, effective_from=changed_on_25th,
)

# “26 日时知道什么”和“现在知道 26 日实际生效什么”分别查询。
then_known = memory.core_at(scope, "relationship", knowledge_at=on_26th, fact_at=on_26th)
now_known = memory.core_at(scope, "relationship", knowledge_at=on_27th, fact_at=on_26th)
```

`core_field_revisions` 保存每次补丁实际修改的字段值、依据、有效起点和系统记录时间。只改称呼不会把名字的生效时间一起重写。查询先限制系统获知时间，再逐字段选取事实时间上已经生效的最新变更；同一有效起点由较晚获知/较高版本修订覆盖。晚到的更早变更不会覆盖已经在更晚事实时间生效的当前值。

`effective_from` 默认当前时间，显式传入时须不晚于记录时间；未来预约变更尚不支持。读取早于首次记录时返回 None；已有核心但事实时间早于字段设定时返回空字段投影。字段始终应用当前遗忘账本，历史回放不会重新暴露已被遗忘的值。身份保持用户/角色/领域隔离，关系再按 relationship_id 隔离。

可信宿主可以调用 `session.historical_core(kind, knowledge_at=..., fact_at=...)`，获得带 `temporal.historical=true` 的历史资料及本会话 `mref`。模型沿用现有 `memory_read` 展开；该接口不会把历史身份替换进当前 bootstrap，也不授权修改核心。来源仍须通过当前权限与撤销检查。

## 摘要版本与正文回滚

```python
versions = memory.summary_history(summary_id, scope)
receipt = memory.restore_summary(
    summary_id, revision=1, scope=scope,
    expected_version=versions[0]["revision"],
    reason="恢复已经核对的旧版摘要正文", idempotency_key="restore-summary-001",
)
```

`summary_revisions` 为正文、来源、算法和生命周期状态的实际变化追加版本。无内容变化的后台重建不虚增版本；显式恢复产生新的恢复版本，旧日志保留。历史检查包含 revision、operation、recorded_at、available 和 snapshot；来源变化、撤回或遗忘后，历史正文保守隐藏为 `snapshot=None`。

`restore_summary` 只接受历史 active 版本，并重新核验所有来源存活、作用域和原 `source_digest`。来源变了就拒绝发布旧摘要，不能借回滚绕过遗忘。恢复检查当前版本，发布正文、版本日志、幂等回执与审计记录在同一事务；失败全部回滚。同 key 的同参数重试返回原操作回执，参数变化拒绝。回执描述已经发生的恢复操作，不表示该摘要以后永远保持 active。

旧 `rollback_summaries(ids, scope)` 继续只表示撤销卡片；`restore_summary` 才会恢复指定历史正文。两种操作的含义独立。

## 原子批量审批

模型继续通过已有 `memory_core_propose` / `memory_request_change` / `memory_checkpoint_propose` 生成单项提议。可信宿主核验是同一次用户请求后组批：

```python
batch = session.create_proposal_batch(
    [{"kind": "core", "proposal_id": identity_proposal_id},
     {"kind": "change", "proposal_id": fact_correction_proposal_id}],
    reason="用户同一次请求的相关修改", idempotency_key="batch-001",
)
receipt = session.approve_proposal_batch(
    batch["batch_id"], user_event_id=actual_user_event_id,
    reviewer="trusted-host", reason="已独立核对整批修改及依据",
)
```

批次最多 20 项，必须绑定同一用户请求事件、精确 scope 与 conversation。拒绝重复目标、混入已处理/失效提议或不同轮请求。整库遗忘必须单独审批，不允许混入批次。

审批先复核全体策略、目标、版本、依据及过期状态；结构化替换的向量在写事务之外预计算。重新进入事务后再次复核，再依次执行每项并逐项检查内容来源；本批前项如果撤销了后项依据，整批失效，已经写入的修改与单项回执全部回滚。最终批次回执、单项终态及全部变更原子提交。

`get_proposal_batch` 检查状态；`reject_proposal_batch` 整批拒绝。终态审批幂等，重试必须保持同一决定、审批人和原因。并发或模型计算失败返回可重试错误；来源/版本冲突返回 invalidated 回执，单项提议不会留下部分应用。没有新增模型自审批入口或请求上下文常驻目录。

## 迁移、持久化与边界

schema 23 升级为 24 时，从已有核心快照的差异推导字段历史，并为当前摘要建立 `migration_baseline`。旧库没有保存的摘要正文、补丁意图或精确字段时间不能补造；迁移前同值重申无法与未修改字段区分。v0.25 以后新增字段变更按实际补丁逐项记录。直接 SQL 改写属于可信管理行为，不能宣称拥有完整修订记录。

完整及作用域快照包含新表；现代快照导入不重新推导历史，旧快照导入才建立基线。scope 清除同时清理字段历史、摘要日志、恢复回执和批次载荷；原始外部备份及已发送模型上下文仍需宿主清理。历史读取不等于完整物理擦除。

新增 25 项验收覆盖双时间差异、部分补丁、晚到更早变更、同起点修订、遗忘屏蔽、LLM 历史引用、正文恢复、版本冲突、原子失败、旧库迁移、快照、批量审批、事务外编码与批次内依据冲突。实际 Harness 审批界面、未来预约核心及更一般事务编排不属于本工作包。

2026-10-27 验证：完整 581 项回归通过。已构建 `memorydreaming-0.25.0-py3-none-any.whl` 并逐项核对包内源码；临时目录独立安装后，由新进程验证核心双时间、摘要历史恢复、持久批次审批/幂等重试、`memory_read` 历史引用和审计链。未调用模型 API，也未改动正在运行的 Harness。
