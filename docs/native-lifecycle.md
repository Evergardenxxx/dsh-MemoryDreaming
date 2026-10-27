# 原生插件生命周期与桥接进程监管

原生插件 v0.12，Python 包仍为 0.22.0，数据库仍为 schema 21。本轮不接入真实 tokenizer，不调用 DeepSeek API，也不重启正在使用的应用。工程目标是取消与故障后能继续工作、回合数据不串写，以及重载后不累积工作进程或退出监听。

## 进程与请求

`lib/bridge-client.js` 负责监管一个共享、顺序执行的 Python JSONL bridge。状态为未启动 → 运行 → 停止中 → 可重启，卸载后为关闭。每个工作进程有独立记录和缓冲区，请求绑定记录身份；旧 stdout、stderr、退出事件不能影响替代进程。

请求在排队前校验取消、队列容量和 UTF-8 编码大小。已经取消的请求不启动进程。正常退出（包括 exit code 0）但没收到回包时，所有绑定该进程的请求立即失败；不会因“退出成功”无限等待。stdin 的异步 EPIPE、启动失败、无效 JSON、无效响应结构、过大响应都使进程退出并清理请求。

| 配置 | 默认 | 有效范围 |
|---|---|---|
| `bridgeTimeoutMs` | 15000 ms | 100..120000 ms |
| `bridgeShutdownMs` | 2000 ms | 100..10000 ms |
| `bridgeMaxPending` | 64 | 1..256 |

异常配置回退默认值。请求截止时间覆盖等待重启、排队与回包；stdin 单行上限 1 MiB，stdout 单行/未闭合缓冲上限 2 MiB。已有工具/历史正文不会经 bridge 发送。首次加载较慢的本地嵌入模型可能需要显式增加截止时间。

已经发出的请求被取消或超时时，监管器终止整个 Python 进程，清除本进程所有 pending、timer、abort listener 和输出缓冲。取消者收到 `ABORTED`，超时者收到 `BRIDGE_TIMEOUT`，同进程其他在途请求收到 `BRIDGE_RESTARTED`。这是共享串行 worker 的明确边界，不能声称只取消了一个线程。新请求必须等待 OS `close` 确认旧进程退出，不能与未确认退出的旧进程重叠。确认超时返回 `BRIDGE_STOP_FAILED`；迟到的 close 确认允许之后恢复。

监管器不自动重放任意写请求。终止进程不会撤销已经提交的事务，调用方不能把“无回包”理解成“未写入”。用户事件用相同 message_id 幂等重试；投递记录用相同 message_id、user_event_id、正文和状态重试。SQLite 的原子事务、既有幂等键与租约规则继续负责已提交数据和后台任务恢复。

卸载先给确认投递任务一个有限的清理窗口，再排队 shutdown。shutdown 回包后也等待 OS close；后台阻塞不退出时强制停止。多次 dispose 返回同一 Promise，关闭后不重新启动。正常关闭清理退出监听；关闭进行中仍保留进程退出时的强制停止兜底。此机制监管直接 Python 子进程，不保证清理第三方模型库另行启动的任意子进程树。

## 回合与取消

用户消息先通过零预算 prepare_turn 持久化，再恢复/检索。该落库操作不继承生成取消信号，受会话 lifetime 和自身截止时间约束，避免取消回答顺带丢掉实际用户输入。恢复/检索同时绑定生成信号与会话 lifetime。供应商元数据查询也有有界等待，即使第三方回调忽略取消，旧查询结果也不再可发布；这不等于能强杀任意 JavaScript 扩展的底层操作。

异步返回在修改状态、清空记忆、安装记忆前复核：插件未卸载、session 对象未关闭、state 仍是当前映射、pending user 仍相同、请求 revision 未变化、信号未取消。旧回合的错误处理不能清掉新回合资料。关闭 session、替代同 ID session、turn/end 和新请求会使相应旧结果失效。

热重载/恢复可以直接进入工具步骤，没有新 pending input。若有效 surface 仍保留真实 `source.kind=user` 消息，则重新绑定最后一条，使用原 message_id 找回同一用户事件，重新签发记忆引用并替换自己的记忆节点。不会把压缩摘要或任意 user-role 内容当成真实用户。如果真实用户消息已被宿主压缩/移除，这个回退不会绕过 surface 去读取旧日志；需要下一条真实用户输入或宿主显式提供绑定。

## 实际回复与投递回执

仅从可信 `assistant/message` 已提交事件提取 text block，不收集 reasoning、流式猜测前缀或工具调用。事件提供 turn 时必须匹配当前 turn；旧 assistant/message 和 turn/end 不能写入新回合。

- 正常 `agent/turn-stopping` 等待 after_delivery 完成。
- 取消/异常路径的 `turn/end` 会保存最后一条已提交可见文本；`interrupted=true` 或中断终止路径使用 interrupted 状态。
- session 关闭和插件卸载也尝试保存已有可见文本，不继承已经取消的生成信号。

投递请求在排队时冻结正文、消息 ID、用户事件 ID 和状态，不在异步回调中读取后来变化的 state。只有回执成功才标记已保存；并发正常/中断/卸载触发合并为同一个请求。退出、重置、管道错误或超时产生的不确定结果，只用原始请求幂等重试一次；业务错误不自动重试。不透明 used_references 仍为空，不伪造模型用了哪些记忆。

本版只在回合终止时保存最后一条已提交回复，未建立跨进程持久 outbox 或导入完整宿主历史。整个 Harness 被强制结束后、尚未收到落库回执的投递仍可能丢失；未提交到 assistant/message 的 UI 流式片段也不猜测恢复。模型已看到的旧上下文与外部备份撤销仍不在此机制内，`full_forgetting=false`。

## 压缩与辅助请求

安装版源码实际发出 `compaction/start/end` session 事件，不保证发出旧兼容 `agent/context-compressing` / `session/context-compressing`。插件收到 start 时尽力保存现有活动 checkpoint 到 handoff，用 compactionId 幂等关联；通知不能被 await，无法保证压缩提交前落库。兼容的显式压缩钩子仍可等待 pre_compress，并用 expected_version 更新活动。

压缩期间摘要器可能复用会话 ID 和 turn signal，因此完整请求守卫暂不拦截压缩摘要调用。end 后恢复普通请求核验。压缩结果不成为用户事实、人格设定或经审批核心。新请求使用压缩后的有效 surface 重算预算，不复制完整旧历史。

## 验证与剩余边界

自动验收入口：

```powershell
python -X utf8 -m unittest tests.test_bridge_supervisor tests.test_deepseek_harness_plugin -q
python -X utf8 -m unittest discover -s tests -q
```

进程监管测试使用真正独立 OS 子进程，覆盖挂起、已取消、请求超时、exit 0、半行退出、无效/过大响应、缺失 Python、队列/输入限制、卸载、旧输出隔离，以及实际 Python bridge 强杀后恢复。专门的故障 worker 在真实 SQLite 提交后立即退出且不返回回执，验证用户事件和投递重试不重复。

插件测试使用对照安装版接口顺序的 Cordis fixture 和真实 Python bridge，覆盖已显示中断前缀、排除 reasoning、旧 turn 拒绝、迟到元数据、取消/关闭期间查询、元数据超时、热重载、压缩通知及反复重载清理监听。这些不是实际桌面应用验收。

2026-10-27 验证：完整回归 525 项通过，其中原生插件与进程监管 33 项。已构建原生插件 `local-memorydreaming-0.12.0.tgz` 与 Python `memorydreaming-0.22.0-py3-none-any.whl`，逐项核对包内文件与源码。在临时目录独立安装 wheel、解包插件，确认 Python 从安装目录导入，并通过跨窗口核心恢复、中断投递、热重载、进程超时后重启及退出监听清理；运行期间未调用模型 API。

仍待完成：真实主程序版本中的完整重启/压缩/取消/异常退出/热更新验收；持久投递 outbox 和故障告警；任意第三方模型进程树监管；独立 SDK 的本地嵌入硬超时。真实 tokenizer 和抽取/嵌入质量按用户安排暂不开展。


v0.13 更新：持久磁盘 outbox、重启重放、投递时间保存、遗忘代次 guard 和固定故障码遥测已实现。先前本文件所列“未建立持久 outbox”是 v0.12 的历史边界，现状以 [运维与恢复](operations-and-recovery.md) 为准；实际桌面应用完整验收仍未完成。

v0.14 用安装版实际运行时验收后修复卸载：Cordis4 通过 `ctx.effect` 返回的清理函数释放插件，不会自动发出 dispose 事件；优先使用 effect，只有旧接口回退事件。真实安装版 agent、工具步骤、压缩、取消、供应商失败、卸载重载和进程退出/JSONL 恢复已通过隔离验收，供应商为确定性本地适配器。详见 [安装版运行时验收](installed-harness-acceptance.md)，不代表当前用户 GUI/登录模型已实测。
