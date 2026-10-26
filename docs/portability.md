# 便携快照与导入预检

快照用于可信宿主或运维备份，不会进入模型上下文，也不注册为 LLM 工具。它保存主记录、来源、作用域、遗忘账本、检查点、handoff 和 durable journal；向量索引与 FTS 索引属于可重建派生物，会在 manifest 中列入 `derived_tables_to_rebuild`。

```python
runtime.export_snapshot("backup/memory.snapshot.json")
report = runtime.preflight_snapshot("backup/memory.snapshot.json")
```

`preflight_snapshot()` 是只读 dry-run，会检查格式/schema、表摘要、来源引用、journal 完整性、ID 冲突、遗忘 HMAC 密钥和作用域 mutation generation。

作用域快照只包含该作用域及其子活动，并明确报告 `PARTIAL_JOURNAL`，因为哈希链是全局链，子集不能冒充完整审计链。密钥或 generation 不一致时预检失败，防止恢复旧备份后让已遗忘事实重新出现。

当前版本只导出和预检，不自动覆盖数据库；报告里的 `apply_supported=false` 是有意的安全闸门。后续要增加显式迁移计划、残留报告和人工确认后的原子切换。
