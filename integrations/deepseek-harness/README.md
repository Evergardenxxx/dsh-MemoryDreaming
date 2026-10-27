# MemoryDreaming for DeepSeek Harness

这是一个原生 DeepSeek Harness Cordis 插件。它直接监听 Harness 的用户消息、模型请求和回合结束事件，由本地 Python 记忆引擎负责恢复、检索与持久化。

## Bundle 内容

- `cordis.patch.yml`：把原生 `memorydreaming` 插件挂载到 Harness。
- `lib/index.js`：Cordis 插件入口；不注册 MCP 工具。
- `locale/en.json`、`locale/zh.json`：插件管理器显示名称和描述。
- `icon.svg`：插件图标。

插件使用 `C:\Python314\python.exe` 在本地启动一个受控 JSONL 桥接进程，并把身份绑定为 `u1 / c1 / r1`。数据库位于仓库的 `.memorydreaming/deepseek-harness.db`，不会写入 Harness profile 目录。桥接协议不注册模型工具，也不把 MCP schema 放入上下文；默认同时启动独立连接的后台整理器，自动处理抽取、去重和摘要。

抽取器默认是零依赖规则版。若要启用 DeepSeek 抽取，在插件配置中设置 `extractor: "deepseek"`，并在 Harness 进程环境中提供 `DEEPSEEK_API_KEY`；可选配置 `MEMORYDREAMING_DEEPSEEK_MODEL`、`MEMORYDREAMING_DEEPSEEK_BASE_URL`、`MEMORYDREAMING_DEEPSEEK_TIMEOUT` 和 `MEMORYDREAMING_DEEPSEEK_MAX_TOKENS`。

插件 v0.10 支持可选本地中文语义模型：`embeddingProvider`、`embeddingModelPath`、`embeddingRevision`、`embeddingDim`、`embeddingQueryPrefix`、`embeddingDocumentPrefix`、`embeddingMaxSeqLength`、`embeddingBatchSize` 和 `vectorMinScore` 会传入 bridge 的环境。未填写时继承进程环境，默认使用哈希。模型依赖和本地权重需预先准备；配置指纹升级后需要 `reindex`。配置、评估和故障回退见 [语义嵌入指南](../../docs/semantic-embedding.md)。

## 安装

在 DeepSeek Harness 的插件管理器中安装本目录的绝对路径。安装后重载或重启应用。首次启动会自动创建数据库文件；插件在每个用户回合开始时调用 `HostAdapter.prepare_turn` 注入有限预算的身份、关系和相关记忆，在回合结束时调用 `after_delivery` 保存实际显示的助手回复。检测到 `agent/context-compressing` 或 `session/context-compressing` 时会调用 `pre_compress`，把当前活动压缩成短 handoff，供下一窗口 bootstrap 恢复。

### 上下文占用

MemoryDreaming 不注册工具，因此不会增加工具 schema。它只按 `maxTokens`（默认 1200，插件硬上限 4096）注入当前回合记忆；超出范围的配置会回退到 1200。如果总上下文仍然很大，应在 Harness 的上下文面板按命名空间检查其他 MCP server（例如 `mcp__rea__*`）。

### 当前限制

- 插件依赖本机 Python 和仓库路径配置，尚未打包成跨机器安装器。
- 插件无法可靠知道模型实际使用了哪条不透明引用，因此投递回执暂不增加 `used_references` 热度。
- 核心候选审批仍需要可信宿主确认；后台抽取可以选择 DeepSeek，真实中文抽取质量仍需验收。
- 如果当前 Harness 版本没有暴露上下文压缩事件，可由宿主自己的压缩回调向桥接进程发送 `{"op":"pre_compress", "conversation_id":"...", "state":{...}, "expected_version":N}`；插件不会猜测或伪造压缩状态。
- 可信宿主也可以发送 `{"op":"prefetch", "conversation_id":"...", "query":"..."}` 预热当前窗口缓存；该操作不会向模型返回记忆正文。
