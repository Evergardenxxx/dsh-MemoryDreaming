# DeepSeek 共享请求调度（v0.21 / schema 21）

原来的 Retry-After 只延后失败任务。一条请求收到 429 后，同一批后续事件仍会访问同一账户，多个窗口和 worker 也会各自重试。现在通过同一 SQLite 库中的请求闸门协调，每个实际请求（包括格式修复）都必须先领取名额。

## 共享边界与配置

共享键是数据库密钥对规范化端点及 API key 的 HMAC，不含 model、事件、用户 ID 或正文。同一端点及凭据的不同 DeepSeek 模型共用闸门；换凭据/端点或使用另一数据库会形成独立配额域。数据库不保存原始凭据、端点或请求正文。调用者不能从模型工具指定配额域。

| 环境变量（前缀 MEMORYDREAMING_） | 默认 | 含义 |
|---|---|---|
| DEEPSEEK_MAX_CONCURRENT | 1 | 同时在途请求上限，1..32 |
| DEEPSEEK_MIN_INTERVAL_SECONDS | 0 | 两次请求起始时间的最小间隔，0..3600；0 表示不额外限频 |
| DEEPSEEK_COOLDOWN_SECONDS | 5 | 429 的最小共享冷却，0.1..86400 |
| DEEPSEEK_REQUEST_LEASE_SECONDS | 120 | 在途名额租约，1..3600；文件库独立连接续租 |

这些变量由 `DeepSeekClient.from_env()` 读取，原生插件启动 bridge 时继承宿主环境；引擎不自动加载 `.env`。SDK 可显式传 `DeepSeekSchedulingPolicy(...)`。多个进程配置不同时，采用最小并发数和最大起始间隔，避免较宽松进程绕过已有约束；放宽限制需由可信运维在无在途请求时显式调用 `scheduler.reconfigure()`，服务端冷却与已预订的起始时间继续保留。

## 请求算法

1. 短写事务合并账户策略，清理已过期名额，读取共享 not_before、next_start_at 及在途数。
2. 未到冷却/间隔时间或名额已满，返回 `ExtractionDeferred(retry_at)`。名额繁忙时最多一秒后重查，不阻塞 worker 等待。
3. 有额度则写随机 request_token 和租约，并预订下一次最早起始时间；提交事务后才访问 DeepSeek。
4. 文件库用独立连接心跳续租。请求完成后复核 token 和有效期并释放；成功结果若已失去租约则丢弃。异常和 KeyboardInterrupt 也释放本次名额。
5. HTTP 429、以及带 Retry-After 的可重试错误，把共享 not_before 单调推进到“当前时刻 + 最大冷却”。另一个较早请求成功或返回更短的冷却都不能缩短它。

网络阶段不持 SQLite 写事务，也不在调度器里 sleep。进程被终止后，请求名额只在租约到期后回收；旧 token 不能续租或删除新请求。租约和心跳不能证明已终止远端 HTTP 执行：进程暂停或网络分区超过租约时可能出现远端重叠，当前没有供应商侧取消或全局硬并发保证。

## 队列算法与格式修复

`run_pending_jobs()` 以及后台整理器自动给 DeepSeekExtractor 使用共享调度。规则抽取、去重和摘要不占供应商名额；原生 Cordis 插件、模型工具目录与默认 1200 token 记忆预算保持原状，调度元数据不注入聊天。

纯等待时，任务使用原 claim_token 原子退回 pending、撤回本次领取的尝试数，并把 available_at 延后到 retry_at。重启后继续等待；即使 max_attempts=1，多次等待也不会将任务耗尽。`ConsolidationReport.deferred` 计数这些重排，后台批次遇到延后即退出本次内循环，按正常唤醒间隔继续。

已向供应商发过请求、但候选格式错误时，修复请求仍要再次领取名额。若修复没有配额，则保留这次真实尝试，按队列退避延后，并同时计入 failed 与 deferred；到尝试上限仍会 failed。下一次从原事件重新抽取，不持久保存错误输出，不恢复半次修复。这样配置起始间隔时可能减少即时修复机会，但不会为等待配额无限重置真实失败的尝试数。

真正的 429 仍按原队列失败策略消耗一次尝试；共享闸门阻止其他未发请求的任务同时失败。遗忘或取消发生后，过期 token 不能通过延后释放重新打开任务。记忆、索引和任务完成仍沿用既有原子提交与来源代次复核。

独立直接调用 `extractor.extract(event)` 或无库评估不自动获得跨任务配额。可信 SDK 可显式使用：

```python
client = DeepSeekClient.from_env()
extractor = DeepSeekExtractor(client)
scheduler = DeepSeekRequestScheduler(runtime.db, runtime.clock, client)
drafts = extractor.extract_scheduled(event, scheduler)
# 无名额时由调用者处理 ExtractionDeferred；候选仍须经标准形成/提交链。
```

生产接入优先使用队列入口，避免把候选直接写入库。`scheduler.status()` 提供不含凭据/端点/正文的运维快照，供宿主查询；它不是 LLM 工具。

## 存储与验证

schema 21 新增 provider_limits 和 provider_requests，旧 schema 20 打开后原子补表。它们不推进检索内容 generation，也不随便携快照导出或导入。清除某用户的记忆不会重置账户冷却；这些表只保存 HMAC、随机 token、时间与数值配置。

新增 25 项回归，覆盖两连接并发领取、跨模型共享、凭据/端点隔离、冷却持久化、旧成功不得清除新冷却、起始间隔、保守策略合并、请求续租与旧 token、真实进程崩溃回收、网络阶段可写、队列等待计数、修复计数、遗忘竞态、后台恢复、脱敏、快照隔离和 schema 20 升级。还修复 worker 直接使用 ZoneInfo 的不一致：缺少 IANA 时区数据时使用已有固定偏移降级，避免干净 Windows 环境的后台线程停止。验证不访问外网。

全量 487 项 unittest、compileall、原生插件 Node 语法及 diff 检查通过。v0.21 wheel 的 45 个源码/数据文件与仓库一致；独立安装后用模拟 HTTP 429 验证两任务共享冷却、重启后到期恢复、创建两条记忆并检索成功，且快照排除实时请求状态。没有调用真实 DeepSeek API 或验证真实账户吞吐。

机制参考：[Celery rate_limit](https://docs.celeryq.dev/en/stable/userguide/tasks.html#Task.rate_limit) 明确限制默认按 worker 实例计算，共享 API 配额需要统一调度。这里使用单机 SQLite 短事务协调，不需要引入外部消息队列。

当前限制：配额只覆盖同一数据库的调度入口，没有跨数据库/其他应用的账户协调、token/minute 计量、分布式队列、公平调度或持久长期配额趋势；没有硬 HTTP 总时限与供应商侧取消。真实 DeepSeek 账户的吞吐和限流行为仍待验收。
