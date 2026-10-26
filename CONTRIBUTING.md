# 贡献指南

MemoryDreaming 是一个零运行时依赖的 Python 记忆引擎。提交改动前，请先在 Python 3.10 以上环境运行：

```bash
python -m compileall -q memorydreaming tests
python -m unittest discover -s tests -q
git diff --check
```

涉及 SQLite 表结构时必须提高 `memorydreaming/storage/database.py` 的 `SCHEMA_VERSION`，并确认旧数据库可以重新打开。派生表（FTS、向量、实体和检索缓存）不能成为事实来源；任何检索候选最终都要经过作用域、状态、来源和遗忘校验。

涉及 LLM 或 Harness 接入时，模型输出只能作为候选。核心、纠错、遗忘和检查点等写操作必须经过可信宿主审批；不要把记忆正文、快照或修订历史自动注入模型上下文，也不要把插件改成 MCP 工具。

新增功能应同时补充回归测试和文档，并在提交信息中说明行为变化。DeepSeek 抽取器目前只支持 DeepSeek；不要在核心引擎中加入供应商 SDK 或运行时依赖。
