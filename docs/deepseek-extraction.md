# DeepSeek 抽取工程与中文评估

Python 包 v0.17 的抽取器使用 DeepSeek `chat/completions` JSON object 模式，依赖标准库。DeepSeek Harness 仍使用原生 Cordis 插件与本地 JSONL bridge；这次没有增加 MCP 工具或聊天启动时的上下文。

## 调用与写入边界

`DeepSeekExtractor.from_env()` 读取 `DEEPSEEK_API_KEY`，以及可选的 `MEMORYDREAMING_DEEPSEEK_MODEL/BASE_URL/TIMEOUT/MAX_TOKENS`。只有用户事件会发送给模型；助手自述与系统消息直接跳过。`.env.example` 是配置清单，引擎不自动加载 `.env`。

模型只提出候选，后端绑定事件、scope、说话者和抽取版本。当前实际契约见 [DeepSeek 候选 Schema](contracts/deepseek-extraction.schema.json)，由测试核对与运行时一致。四类正文还会经过 `MemoryFormationPolicy` 校验。模型的临时 `candidate_key` 不参与持久去重；后端以类型、正文、来源区间、认识论/模态、规范化时间及精度生成 SHA-256 键。同一内容在重试时换随机标签不会产生新记忆，同义改写的自动归并仍有待评估。

`source_span` 使用 Python Unicode 码点下标，左闭右开。提示词要求同时提供 `source_quote`；解析器收到引句时逐字核对，偏移错误且引句在原文唯一出现时可重新定位，多义或不存在则拒绝。为兼容旧 callable，缺少 quote 时仍接受合法范围，此时不能证明候选内容被该片段支持。JSON、下标和引句匹配都不能证明语义真实。

LLM 未提供事件时间时保留 `occurred_start=null, occurred_precision=unknown`，不会把记录消息的当前时间伪装成经历发生时间；记录时间仍正常保留。已知时间转换到 UTC；倒置区间、非法数字、重复 JSON 键、重复候选和未允许的权限字段都会拒绝整批。

## 失败策略

| 情况 | 处理 |
|---|---|
| HTTP 429 | `RATE_LIMITED`，交由队列延迟重试 |
| HTTP 408/5xx、网络中断、超时 | 可重试；超时记录 `TIMEOUT` |
| HTTP 401/403 | `AUTHENTICATION`，当前任务直接 failed，等待宿主修正配置 |
| HTTP 402 | `QUOTA_EXCEEDED`，当前任务直接 failed |
| 其他 HTTP 4xx | `INVALID_REQUEST`，当前任务直接 failed |
| `finish_reason=length` | `OUTPUT_TRUNCATED`，不接受半截候选；按队列上限重试，需宿主检查输出预算 |
| `finish_reason=content_filter` | `CONTENT_FILTERED`，当前任务直接 failed |
| 非法候选/非 JSON 输出 | 最多一次格式修复，再失败则进入队列退避 |

没有在 worker 内 sleep 或立即循环重发网络请求。服务端 `Retry-After` 支持秒数和 HTTP-date；上限 86400 秒，取它与本地指数退避的较大值，保存到现有 `available_at`，重启后继续遵守。该机制是每任务退避，尚不是跨任务的全局请求配额调度器。

格式修复最多增加一次 API 调用，只重发原事件与固定格式约束，不把出错的模型正文重新拼进指令。单任务尝试默认最多 3 次，因此持续格式失败最坏可调用 6 次 API；可以用 `DeepSeekExtractor(client, repair_invalid_output=False)` 关闭修复。传输响应上限 2 MiB，候选 JSON 上限 256000 字符；HTTP 错误正文不读取、不写日志，异常链不回显凭据和供应商原始错误。全部候选校验通过后才能开始持久写入。

## 可复现评估

新增内置 24 条中文合成回归数据，覆盖四类候选、否定、引用、愿望、虚构、说话者、Unicode 来源位置及提示注入。它是独立于抽取正则代码的固定标签文件，**未经独立人工复核，不是研究基准或最终保留测试集**。复杂表达仍需要真实对话标注和人工抽查。

离线规则基线：

```powershell
python -X utf8 -m memorydreaming eval-extraction --extractor rules
```

真实 DeepSeek 评估需要已配置的 API key；这会调用外部 API。可先创建本地结果目录，再执行：

```powershell
New-Item -ItemType Directory -Force .memorydreaming/evaluation
python -X utf8 -m memorydreaming eval-extraction --extractor deepseek --output .memorydreaming/evaluation/score.json --save-predictions .memorydreaming/evaluation/predictions.json
python -X utf8 -m memorydreaming eval-extraction --extractor replay --predictions .memorydreaming/evaluation/predictions.json
```

评估不打开长期记忆库。默认报告只包含 case ID、标签、计数、错误码和数据集指纹；只有显式保存 predictions 才写候选正文。replay 不调用网络，并核对数据集 ID/内容指纹。存在失败时 CLI 返回 1，避免把“成功跑完评分”误作“模型通过验收”。

评分采用标注字段、模态和原文片段的一对一匹配，输出 precision/recall、额外/漏失候选、case 通过率和调用失败数。在标注禁止 occurred 的反例上另计错误事实化比例，并单列调用失败数。字段相同但来源区间不含标注片段，不算命中；一条候选不能匹配两个事实。

内置空标签遵循保守弃记策略；正确标成引述/愿望的额外候选也可能被计为标签外输出。字面不同的正确改写也可能失分，因此这些指标是**标签匹配指标**，不能宣称最终回答正确率或真实幻觉率。

当前规则基线为 14/24 case 通过、4/14 标签命中、precision 80%、recall 28.6%，11 条禁止 occurred 反例均未错误事实化。这说明当前规则覆盖有限；本轮未进行真实账号调用，不能用模拟 API 测试或该基线替代 DeepSeek 质量验收。完整结果见 [规则基线报告](evaluation/extraction-zh-v1-rules.json)。
