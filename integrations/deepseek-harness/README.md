# MemoryDreaming for DeepSeek Harness

原生插件 **v0.17**，配套引擎 **v0.30 / schema 27**。通过 Cordis 生命周期和本地 Python JSONL bridge 保存及恢复长期记忆，不注册 MCP 工具。默认不参与普通窗口。

## 桌面记忆管理

重载插件后，Harness侧栏提供“记忆管理”：四类记忆搜索与来源、五类已有候选审批、纠错/遗忘、空角色初始化、角色概览、睡眠梦境和诊断。没有聊天窗口也可管理固定角色资料；选择窗口后可以提交开启/关闭/睡眠/苏醒指令。入队不等于完成，以后续状态及对话回执核对。

页面使用拥有者原生RPC，不新增HTTP服务或模型上下文；管理连接与对话连接分开，关闭页面不会重启对话的记忆工作进程。角色完整编辑、档案导入导出尚待补齐，详见[桌面指南](../../docs/desktop-management.md)。

## 开启、睡眠与苏醒

在需要记忆的窗口发送整条独立指令：

| 指令 | 行为 |
|---|---|
| `记忆开启` 或 `/memory on` | 只启用当前窗口，恢复已确认身份与关系 |
| `记忆关闭` 或 `/memory off` | 停止当前窗口记录、检索和注入；既有记忆保留 |
| `记忆睡眠` 或 `/memory sleep` | 当前模型逐批审阅本窗口已保存的对话，保存虚构梦和可逆巩固结果，再进入睡眠 |
| `记忆苏醒` 或 `/memory wake` | 启用当前窗口，执行连续性自检，并提供最近有效的梦与短回顾 |

新窗口始终需要单独开启或苏醒。同一持久会话重启/重载后保留开关状态；引述、普通句子中的指令、模型回复和工具结果不触发。睡眠只审阅启用后已经保存到本地的内容，不暗中导入此前聊天。梦标记为 `generated_fiction`，不能当作实际共同经历或人格依据。详见 [窗口与睡眠](../../docs/window-sleep-wake.md)。

## 长期角色指令

先开启当前窗口，再发送独立的中文指令和完整 JSON 对象。这些是用户直接确认的管理命令，参数必须与已保存的用户原话一致；模型生成的 JSON 不具有审批权。

```text
记忆自传 {"title":"海边故事的开端","text":"我们把睡前海边故事约定为共同的聊天习惯。"}
记忆承诺 {"text":"明晚继续灯塔故事","due_at":"2026-10-28T20:00:00+08:00","category":"continuation"}
记忆偏好 {"key":"安慰方式","value":"先倾听，再问我是否需要建议","context":"sad"}
记忆成长 {"field":"storytelling","value":"温柔缓慢地讲述"}
记忆自检
```

无参数发送 `记忆自传`、`记忆承诺`、`记忆偏好`、`记忆成长`，分别查看当前已确认记录。自传是带来源的叙事投影，承诺仍是未来安排；成长仅支持 `response_style`、`storytelling`、`interaction_habit`、`support_style` 四类表达/习惯，不自动更改核心身份。修改现有值须使用回执中的当前版本；成长受冷却和审批约束，冲突会明确拒绝。

完成或取消承诺时，使用“记忆承诺”回执中的真实 `intention_id` 与当前 `version`：

```text
记忆完成 {"id":"替换为真实intention_id","version":2}
记忆取消 {"id":"替换为真实intention_id","version":2}
```

承诺可用 `event_trigger` 等待宿主事件；用户明确确认事件发生时，发送 `记忆触发 {"name":"exam-ended"}`。时间必须带时区，例如 `+08:00` 或 `Z`，缺时区会拒绝。自传还可显式指定 `perspective`、`modality` 和带时区 `occurred_at`；虚构合作内容使用 `modality:"shared_fiction"`，不会提升为实际经历。

## 主动提醒默认关闭

```text
记忆提醒 {"enabled":true,"timezone":"Asia/Shanghai","quiet_start":"22:00","quiet_end":"08:00","min_interval_seconds":86400,"max_per_day":1}
记忆提醒
记忆提醒 {"enabled":false}
```

这只配置本地提醒队列的用户授权、安静时段和频率。**当前原生插件没有定时推送渠道，不会因开启策略就在后台主动发送消息。** 接入方须通过可信 SDK 或内部 bridge 显式领取、发送前复核，再确认投递；稳定 `delivery_id` 用于渠道去重。承诺完成/取消、来源遗忘、暂停或配额限制都会阻止旧提醒。跨设备档案恢复后策略暂停，须重新确认。详见 [承诺与主动性](../../docs/intentions-proactivity.md)。

## 安装与配置

按 [安装说明](../../docs/releasing.md) 运行 `scripts/install.py`，再在 Harness 原生插件管理器中选择生成的 `plugin` 目录，重载或重启应用。安装器创建隔离 venv、明确 Python 路径及 `activationMode: command` 配置，不覆盖已有安装或用户数据库。源码模式使用 PATH 中的 `python`；数据库默认放在 `.memorydreaming/deepseek-harness.db`。

普通抽取默认使用零依赖规则版。可选 `extractor:"deepseek"` 需要单独的 `DEEPSEEK_API_KEY`；睡眠审阅使用 Harness 当前模型路由，可以沿用应用登录态。可选本地语义权重与参数见 [语义嵌入](../../docs/semantic-embedding.md)，不会在启动窗口时下载模型。

默认记忆上限为 1200 个估算 token，配置范围 128..4096。每次请求核对完整历史、指令、工具和输出预留，只替换一个自有资料节点；预算不足跳过注入并保留已授权用户事件。自传、承诺、偏好与成长仅按预算提供短资料，不把全部角色历史常驻上下文。完整线上请求仍标 `estimated`；本地 tokenizer 必须固定模型/资产，不能用 V3 代替当前 V4.1，见 [计量接口](../../docs/tokenizer-integration.md)。

## 生命周期、模块与验收

已提交的可见回复先写磁盘 outbox，再发送 bridge；重启后幂等重放，遗忘 guard 拒绝过期正文。默认 bridge 截止 15 秒，取消或超时可终止共享 Python worker；Cordis 4 使用 effect 清理，旧接口回退 dispose 事件。压缩通知保存 handoff 属尽力处理，不能保证压缩提交前完成。见 [运维恢复](../../docs/operations-and-recovery.md) 与 [生命周期](../../docs/native-lifecycle.md)。

`lib/index.js` 编排生命周期；`native-session.js` 适配消息与记忆节点；`window-control.js` 和 `life-commands.js` 处理真实用户指令；`sleep-coordinator.js` 调度独立分片审阅；`bridge-client.js` 监管进程；`delivery-outbox.js`、`durable-files.js` 负责持久补偿。架构与扩展入口见 [系统结构](../../docs/system-architecture.md)。

安装版真实 Cordis/agent/tool/compaction/JSONL 已通过 **15 条隔离运行时路径**，包含结构化角色命令、自检及有界角色恢复。供应商是确定性测试适配器，未通过这些结果宣称当前登录模型、Electron GUI、新提醒渠道或长期真实质量均已验收。仍需实际用户界面与线上模型验证，详见 [安装版验收](../../docs/installed-harness-acceptance.md)。
