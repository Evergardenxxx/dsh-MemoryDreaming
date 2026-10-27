# 长期角色连续性（v0.29 / 原生插件 v0.16）

长期记忆的目标是让角色跨窗口恢复可核验的身份、关系、共同经历和未完事项。七项扩展已经进入持久数据库、可信宿主 SDK 和原生窗口生命周期；它们不需要加载新的模型工具目录。

## 七项能力及入口

| 能力 | 当前实现 | 详细说明 |
|---|---|---|
| 自传主线 | 有来源的候选、用户确认、视角/虚构标签、修订、撤回及版本历史 | [自传与连续性](narrative-continuity.md) |
| 未完承诺 | proposed/open/completed/cancelled，时间/事件触发，跨窗口恢复 | [承诺与主动性](intentions-proactivity.md) |
| 场景偏好 | 有用户确认的相处方式，default与特定场景覆盖，纠正/撤销不回退旧值 | [陪伴偏好](companionship-preferences.md) |
| 受控成长 | 表达与习惯字段的候选、审批、版本、冷却、拒绝及撤回 | 下文“成长” |
| 苏醒自检 | 身份、关系、自传、承诺、偏好、成长与恢复缺口的有界状态卡 | `记忆自检`、`记忆苏醒` |
| 私密连续性档案 | 全新设备密钥/遗忘代次恢复，导入前预检与导入后指纹核对 | 下文“迁移” |
| 用户允许的主动性 | 默认关闭的频率、安静时段、额度、租约、复核与回执队列 | 宿主接口；实际推送渠道尚未接入 |

普通回合最多申请500个估算token的角色资料，再与核心、检索结果一起受整个记忆预算约束；空资料不增加常驻块。苏醒自检申请600个估算token并随回执总预算裁剪。完整历史、自传、审批记录和私密档案不进入模型上下文。主请求仍执行原生完整请求计量，估算卡片预算不等于线上tokenizer精确计量。

## 原生指令

先在目标窗口单独发送 `记忆开启`。以下指令必须是整条真实用户消息，前缀与JSON之间有空格；引述、代码块、工具结果和角色生成的指令均不构成授权。

```text
记忆自传 {"title":"海边故事的开始","text":"我们约定以后继续讲灯塔故事。","perspective":"shared"}
记忆承诺 {"text":"下次继续灯塔故事","category":"continuation","event_trigger":"故事继续"}
记忆偏好 {"key":"安慰方式","value":"先倾听，再询问是否需要建议"}
记忆成长 {"field":"storytelling","value":"讲故事慢一点，句子短一些"}
记忆自检
```

用户直接给出完整JSON内容时，宿主将原话保存为来源并完成显式确认；这并不证明任意用户陈述是客观发生的事实。自传仍保留视角和模态，成长仍与人格核心分开。普通聊天中的推测只允许产生候选，不能由模型自行批准。

空参数 `记忆自传`、`记忆承诺`、`记忆偏好`、`记忆成长`、`记忆提醒` 分别查看当前状态。回执提供ID和版本；更新时使用对应当前版本，过期版本失败，防止两个窗口相互覆盖。数据很长时本地回执会截断，可通过绑定SDK读取。

| 指令 | 参数与行为 |
|---|---|
| `记忆自传` | `title/text`必填；`perspective`为user/character/shared；`modality`为occurred/shared_fiction。修订还传`entry_id/version`，可选`occurred_at` |
| `记忆承诺` | `text`必填；可选`owner`（user/character/shared）、`category`（promise/continuation/creative）、`due_at/event_trigger`。两种触发条件都有时须同时满足 |
| `记忆完成` / `记忆取消` | `{"id":"intention_实际ID","version":2}`，ID和版本必须从实际回执或查询中取得 |
| `记忆触发` | `{"name":"故事继续"}`，记录用户明确确认的触发事件，不从模型猜测自动触发 |
| `记忆偏好` | `key/value`必填；可选`context`（默认default）与`version`。场景名由宿主显式选择，当前普通原生回合读取default |
| `记忆成长` | `field/value`必填；再次修改传当前`version`，明确快速纠正可传`allow_recent:true` |
| `记忆提醒` | `enabled`必填；可选`version/timezone/quiet_start/quiet_end/min_interval_seconds/max_per_day` |
| `记忆自检` | 不接受额外参数，输出恢复状态与缺口，不补造缺失身份 |

时间必须带时区，例如 `2026-10-28T20:00:00+08:00` 或 `2026-10-28T12:00:00Z`；无时区时间会拒绝。主动策略采用IANA时区，例如Asia/Shanghai；本机缺少该时区数据时明确报错，UTC始终可用。

英文入口为 `/memory autobiography|promise|complete|cancel|trigger|preference|growth|proactive|check`，JSON参数相同。完整示例：

```text
记忆承诺 {"text":"晚上继续讲故事","due_at":"2026-10-28T20:00:00+08:00"}
记忆提醒 {"enabled":true,"timezone":"Asia/Shanghai","quiet_start":"22:00","quiet_end":"08:00","min_interval_seconds":86400,"max_per_day":1}
记忆提醒 {"enabled":false}
```

开启提醒只授权本地队列。当前插件没有定时弹窗或外部消息渠道，以上命令不会自行向外发消息。未来渠道须领取、发送前复核，并以稳定delivery_id去重，成功后才确认回执；投递成功不等于承诺已完成。

## 受控成长

`runtime.growth`只接受四类字段：`response_style`、`storytelling`、`interaction_habit`、`support_style`。每次变化保存固定版本来源、真实用户确认、期望版本、理由、审批者和历史；姓名、关系、事实和审批权限继续走原有核心/事实接口。

```python
candidate = memory.growth.propose(
    scope, "storytelling", "慢慢讲，使用较短的句子", [source_event_id],
    reason="用户明确描述了故事风格", request_id="style-001",
)
memory.growth.approve(
    scope, candidate["proposal_id"], user_event_id=confirmation_event_id,
    reviewer="trusted-host", reason="已独立核对用户确认", expected_version=0,
    request_id="style-001-confirm",
)
```

`propose`不生效。`approve`检查当前版本，同一字段默认24小时内不再次改变；用户明确纠正时由可信宿主设`allow_recent=True`。`revoke`产生撤回版本，`profile`不回退旧值；撤回不是从备份中永久擦除。原生可用新成长指令替换值，候选拒绝/独立撤回等完整管理入口目前由SDK提供。

每范围最多64个未过期成长候选，24小时过期；相同内容与来源按规范签名去重，换理由、请求ID或来源顺序不能绕过拒绝。新用户依据可以形成新提议，保留改变主意的可能。`inspect/profile/history/invalidate_stale`统一复核来源；来源遗忘或失效后隐藏/清除相应投影正文，历史也不能泄漏旧值。

这采用了[LangMem的前台/后台职责分离](companionship-preferences.md)与[Mem0修改历史机制](companionship-preferences.md)，再加本项目的用户审批、来源版本、冷却和遗忘规则。它是可撤销的表达偏好变化，尚未自动从长期互动中决定人格应该如何成长。

## 迁移与核验

档案使用本地CLI，不把私密备份交给LLM处理。推荐放在已忽略的`.memorydreaming/`或仓库之外：

```powershell
python -m memorydreaming --db .memorydreaming/deepseek-harness.db role-card --user u1 --character c1 --relationship r1
python -m memorydreaming --db .memorydreaming/deepseek-harness.db archive-export --user u1 --character c1 --relationship r1 .memorydreaming/xiaomeng.continuity.json
python -m memorydreaming --db .memorydreaming/new-device.db archive-check --user u1 --character c1 --relationship r1 .memorydreaming/xiaomeng.continuity.json
python -m memorydreaming --db .memorydreaming/new-device.db archive-restore --user u1 --character c1 --relationship r1 .memorydreaming/xiaomeng.continuity.json
```

档案是私密明文，含原话、全部导出表及恢复遗忘指纹的私密密钥，不是公开manifest或上下文卡。它拒绝覆盖已有文件；SHA-256用于完整性检查，不是加密或对恶意改写者的身份认证。保管方式应与数据库备份一致，不上传GitHub。

真正空白目标可恢复原密钥/代次；已有记录、回执或删除屏障的目标不会被视为新设备，不会重设遗忘历史。恢复失败整批回滚。跨设备导入的主动策略默认暂停，需要新的用户确认。非空不同密钥库的自动合并未提供。

保留后续删除记录的目标会拒绝不合规旧档案；完全空白的离线设备无法知道档案生成后的新删除请求，须使用更新档案或另行同步最新账本。档案保证角色资料的可恢复性，不能检验主观意识连续。

## 验证范围与后续接入

738项工程回归与15条安装版实际运行时隔离路径已通过，覆盖默认关闭、完整用户命令、来源/版本、并发、遗忘传播、角色资料预算、苏醒以及新设备档案恢复。新版wheel已在带空格路径的独立venv安装，使用安装后的原生插件验证上述指令、常规回合角色恢复和新设备档案核验，恢复策略保持暂停。本轮参考机制和固定来源分别列在[自传](narrative-continuity.md)、[偏好](companionship-preferences.md)、[承诺](intentions-proactivity.md)文档中，没有复制第三方实现。

当前仍待产品接入的是定时推送渠道、图形化审批/档案管理、自动场景选择；真实登录模型与Electron GUI的新指令流程仍需独立验收。既有DeepSeek真实抽取质量、独立中文语义验收及当前V4.1线上计量也继续保留在[待办](open-problems.md)，工程回归不替代这些证据。
