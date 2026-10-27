# MemoryDreaming for DeepSeek Harness

这是一个原生 DeepSeek Harness Cordis 插件。它直接监听 Harness 的用户消息、模型请求和回合结束事件，由本地 Python 记忆引擎负责恢复、检索与持久化。

## Bundle 内容

- `cordis.patch.yml`：把原生 `memorydreaming` 插件挂载到 Harness。
- `lib/index.js`：Cordis 插件入口；不注册 MCP 工具。
- `lib/bridge-client.js`：桥接进程监管、请求截止时间、取消与重启。
- `lib/request-budget.js`：完整请求计量与预算预览。
- `locale/en.json`、`locale/zh.json`：插件管理器显示名称和描述。
- `icon.svg`：插件图标。

插件使用 `C:\Python314\python.exe` 在本地启动一个受控 JSONL 桥接进程，并把身份绑定为 `u1 / c1 / r1`。数据库位于仓库的 `.memorydreaming/deepseek-harness.db`，不会写入 Harness profile 目录。桥接协议不注册模型工具，也不把 MCP schema 放入上下文；默认同时启动独立连接的后台整理器，自动处理抽取、去重和摘要。

抽取器默认是零依赖规则版。若要启用 DeepSeek 抽取，在插件配置中设置 `extractor: "deepseek"`，并在 Harness 进程环境中提供 `DEEPSEEK_API_KEY`；可选配置 `MEMORYDREAMING_DEEPSEEK_MODEL`、`MEMORYDREAMING_DEEPSEEK_BASE_URL`、`MEMORYDREAMING_DEEPSEEK_TIMEOUT` 和 `MEMORYDREAMING_DEEPSEEK_MAX_TOKENS`。

插件 v0.10 支持可选本地中文语义模型：`embeddingProvider`、`embeddingModelPath`、`embeddingRevision`、`embeddingDim`、`embeddingQueryPrefix`、`embeddingDocumentPrefix`、`embeddingMaxSeqLength`、`embeddingBatchSize` 和 `vectorMinScore` 会传入 bridge 的环境。未填写时继承进程环境，默认使用哈希。模型依赖和本地权重需预先准备；配置指纹升级后需要 `reindex`。配置、评估和故障回退见 [语义嵌入指南](../../docs/semantic-embedding.md)。

## 安装

在 DeepSeek Harness 的插件管理器中安装本目录的绝对路径。安装后重载或重启应用。首次启动会自动创建数据库文件；插件在每个模型请求前调用 `HostAdapter.prepare_turn` 恢复有限预算的身份、关系和相关记忆，在回合结束时调用 `after_delivery` 保存实际显示的助手回复。检测到 `agent/context-compressing` 或 `session/context-compressing` 时会调用 `pre_compress`，把当前活动压缩成短 handoff，供下一窗口 bootstrap 恢复。

### 上下文占用

MemoryDreaming 不注册工具。v0.11 对每次模型请求做完整预算预览与发送前检查，计入原指令、历史、工具、当前输入和输出预留；记忆仍受 `maxTokens`（默认 1200，128..4096）限制。只替换一个自有 surface 节点，不反复追加快照。预算不足/未知时保存用户消息并跳过恢复，最终超限则在联网前阻止发送。

窗口与默认输出 cap 优先读取供应商元数据，可选 `contextWindow` 收紧窗口、`outputReserve` 增加输出预留、`safetyMargin`（默认 512）与 `requestOverhead`（默认 64）增加余量。默认 **estimated**，真实 DeepSeek tokenizer 尚未接入；可信宿主可提供请求级计数器，见 [预算接口与边界](../../docs/request-budget.md)。基础请求很大时仍需宿主压缩历史或精简其他工具，插件不会替宿主删除它们。

### 生命周期与故障恢复

v0.12 先保存真实用户输入，再恢复资料；每个 bridge 请求默认 15 秒截止时间。取消已发送的请求或超时会终止共享 Python worker，其他在途请求明确失败，后续请求等待退出确认后重启。正常退出未回包、无效回包与管道错误也会清理等待者。`bridgeTimeoutMs` 可设 100..120000，`bridgeShutdownMs` 默认 2000（100..10000），`bridgeMaxPending` 默认 64（1..256）。

取消路径通过真实 `turn/end` 保存已提交的可见回复，回执不继承生成取消信号。热重载可从有效 surface 中最后一条真实用户消息恢复绑定，重复 message_id 不重复入库。安装版 `compaction/start/end` 也已接入，但通知无法保证压缩提交前 handoff 落库。完整状态机、自动验收及剩余边界见 [生命周期指南](../../docs/native-lifecycle.md)。

### 当前限制

- 插件依赖本机 Python 和仓库路径配置，尚未打包成跨机器安装器。
- 插件无法可靠知道模型实际使用了哪条不透明引用，因此投递回执暂不增加 `used_references` 热度。
- 核心候选审批仍需要可信宿主确认；后台抽取可以选择 DeepSeek，真实中文抽取质量仍需验收。
- 如果当前 Harness 版本没有暴露上下文压缩事件，可由宿主自己的压缩回调向桥接进程发送 `{"op":"pre_compress", "conversation_id":"...", "state":{...}, "expected_version":N}`；插件不会猜测或伪造压缩状态。
- 可信宿主也可以发送 `{"op":"prefetch", "conversation_id":"...", "query":"..."}` 预热当前窗口缓存；该操作不会向模型返回记忆正文。
