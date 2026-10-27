# 贡献指南

MemoryDreaming的基础Python引擎没有第三方运行时依赖，当前处于试用阶段。先阅读[路线图](ROADMAP.md)、[文档导航](docs/README.md)和[开发导读](docs/development-guide.md)，优先解决可复现的实际记忆与使用问题。

## 开发与验证

提交改动前，请先在Python3.10以上环境运行；涉及原生链路还需Node.js，CI使用Node22：

```bash
python -m compileall -q memorydreaming examples tests
python -m unittest discover -s tests -q
git diff --check
```

涉及 SQLite 表结构时必须提高 `memorydreaming/storage/database.py` 的 `SCHEMA_VERSION`，并确认旧数据库可以重新打开。派生表（FTS、向量、实体和检索缓存）不能成为事实来源；任何检索候选最终都要经过作用域、状态、来源和遗忘校验。

涉及 LLM 或 Harness 接入时，模型输出只能作为候选。核心、纠错、遗忘和检查点等写操作必须经过可信宿主审批；不要把记忆正文、快照或修订历史自动注入模型上下文，也不要把插件改成 MCP 工具。

修复和新行为应补充有意义的回归及文档；纯文档/注释或机械移动不要求镜像实现的测试。DeepSeek抽取器目前只适配DeepSeek，基础引擎保持标准库路径；新增可选依赖或供应商范围先讨论实际需求。

## 提交与讨论

功能建议请说明具体场景和可验收的前后行为。PR描述解释问题、改后行为、实际验证和剩余边界；用合成对话/临时库复现，勿附私密数据库、档案、API key或Harness profile。疑似漏洞按[安全说明](SECURITY.md)反馈。

修改公开行为时同步README、专项文档和CHANGELOG；新增目录/文档加入导航。架构变化同步`docs/architecture-overview.md`、`docs/diagrams/architecture.mmd`和`docs/assets/architecture.svg`；主图可简化，但不能把尚未接入的能力画成默认启用。

引入第三方代码/资源前核对许可证并保留归属，不能只在参考表列项目名。研究/机制借鉴记录具体来源与差异。讨论保持尊重，审查聚焦证据、行为和实现。
