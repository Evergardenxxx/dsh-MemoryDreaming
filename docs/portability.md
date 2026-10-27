# 便携快照、预检与原子导入

快照用于可信宿主或运维备份，不会进入模型上下文，也不注册为 LLM 工具。它保存主记录、来源、作用域、遗忘账本、检查点、handoff 和 durable journal；向量、FTS 与跨进程检索缓存属于可重建派生物，会在 manifest 中列入 `derived_tables_to_rebuild`。缓存本身不含正文，导入时也不会迁移。

```python
runtime.export_snapshot("backup/memory.snapshot.json")
report = runtime.preflight_snapshot("backup/memory.snapshot.json")
```

`preflight_snapshot()` 是只读 dry-run，会检查格式/schema、表摘要、来源引用、journal 完整性、ID 冲突、遗忘 HMAC 密钥和作用域 mutation generation。

作用域快照只包含该作用域及其子活动，并明确报告 `PARTIAL_JOURNAL`，因为哈希链是全局链，子集不能冒充完整审计链。密钥或 generation 不一致时预检失败，防止恢复旧备份后让已遗忘事实重新出现。

预检通过后，可信宿主可以显式调用 `runtime.import_snapshot(path)` 原子导入；任何表失败都会整体回滚。相同主键且内容相同的记录幂等跳过，不同内容在预检阶段拒绝。向量、FTS 和检索缓存是派生数据，导入报告会列出需要重建的残留项；摘要的 `withdrawn/rolled_back` 状态属于主投影元数据，会随快照保留。不同遗忘 HMAC 密钥仍默认拒绝直接导入，防止旧账本复活。

schema27完整和范围快照携带自传、承诺、偏好、成长与对应历史/回执。导入的主动策略会禁用，claimed投递退回pending并清空租约；只处理本次导入的作用域/投递，不影响其他关系的策略。范围导出同时保留该用户/角色/领域共享的身份核心，但不授予跨关系的来源可见性。

需要迁移到真正空白的新设备时，v0.29提供额外的私密连续性档案，封装快照与私密HMAC恢复材料；仅在空目标恢复原key/epoch，非空目标的删除屏障保持。普通快照不包含该私密恢复材料，不因新增档案接口而允许任意跨密钥导入。具体命令、保护方式与离线旧备份限制见[角色迁移](long-term-role.md)和[档案契约](narrative-continuity.md)。

schema 20 增加 `change_purge_receipts`：作用域清除后保存无正文的最小审批回执及 HMAC，支持重启后查询和幂等重试。此表也随快照保存；预检仍接受 schema 19 及更早快照缺少该表。它像清除代次一样是保留的审计元数据，不承诺彻底擦除所有标识符，见 [纠错与变更审批](correction-approval.md)。

schema 21 的 `provider_limits` / `provider_requests` 是本库实时运行状态，列在 manifest 的 `operational_tables_skipped`，不导出、不从快照恢复，也不参与内容 generation。导入不会重置目标库已有的冷却或在途名额；不同数据库之间不共享供应商配额。
