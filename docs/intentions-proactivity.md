# 承诺追踪与用户允许的主动性

承诺是有依据的未来安排，不是“已经发生”的语义事实。角色说过“下次接着讲”只形成候选；可信宿主确认用户同意后，才成为可跨窗口继续的 open 承诺。主动提醒另有独立授权，默认关闭。服务不会发送任何外部消息或启动常驻提醒线程。

## 成熟实现的具体借鉴

2026-10-27 实际读取并核对了以下公开实现/文档：

| 来源 | 已核对的机制 | 本项目采用方式 |
|---|---|---|
| [APScheduler user guide](https://github.com/agronholm/apscheduler/blob/05b4e2d69f89e3f8d87c6eec5df71ed1dd5705ec/docs/userguide.rst) | `max_running_jobs` 限制同时运行；`misfire_grace_time` 区分过期任务；coalescing 将错过的多个运行时点合并为一个可执行任务。 | 每条承诺只建立一个稳定的提醒 delivery_id，不因窗口轮询/重启补发一串重复提醒；领取使用租约，频率/每日额度在同一写事务中保留。此服务不是 APScheduler 的包装，也不暗中启动 scheduler。 |
| [Celery task guide](https://github.com/celery/celery/blob/380b4a930224c9ac8248094d3a2e27524caa97f8/docs/userguide/tasks.rst) | 任务应幂等；`acks_late` 表示执行之后确认，但 worker 丢失与重投递仍需有明确策略。 | 承诺变更和投递确认各有持久幂等回执；发送前领取，确认后才 delivered；进程丢失通过租约重领。稳定 delivery_id 必须由宿主交给实际发送渠道作幂等键，不能宣称本地数据库使外部发送 exactly-once。 |
| [Generative Agents planning](https://github.com/joonspk-research/generative_agents/blob/f1f7e1b71054de958ab87660249ee0fd6644e9a9/reverie/backend_server/persona/cognitive_modules/plan.py) | `_long_term_planning` 建立计划，插入活动时 `generate_new_decomp_schedule` 重新分解安排；计划与观测事件分开管理。 | 未完成承诺保留独立状态/触发条件；新安排通过修订重新进入 proposed，不能仅因旧提议曾被接受就沿用新内容的审批权。 |

固定文档内容 SHA-256 分别为 APScheduler `330741fd262460fae33343ad107009056e77d2598073fa0c0bb1fc0a7a99c848`、Celery `8131efc403170bce0b593b8a3f08b9a68d05b2115eb65aaeb7ba202cbf2e9421`、Generative Agents `d0cf387a9015750b05606693d126d1d5e1c2d12b95a85b22937a2b3d8b14042a`。源码未复制进项目，采用的是机制及相应边界。

## 承诺生命周期

服务为 `IntentionsService(runtime)`，由主运行时以同一数据库连接组合。公开 SDK：

```python
proposed = memory.intentions.propose(
    scope, "等考试结束后接着讲灯塔故事", evidence=[actual_user_event_id],
    owner="character", category="continuation", event_trigger="exam-ended",
    request_id="promise-001",
)
accepted = memory.intentions.confirm(
    scope, proposed["intention_id"], expected_version=1,
    confirmation_refs=[actual_user_event_id], reviewer="trusted-host",
    reason="已独立核对用户确实同意", request_id="confirm-001",
)
```

`owner` 为 user/character/shared；它描述安排属于谁，不授予角色向用户强加义务的权利。`category` 为 promise/continuation/creative。`due_at` 必须是带时区 datetime，以 UTC 保存；`event_trigger` 只是固定宿主触发名。两者都存在时必须同时满足；都不存在时仍保留在 `list_open`，但不主动提醒。

创建候选依赖有效事件/记忆来源；确认、修订、完成和取消还需要真实用户事件或用户自述/明确设定依据。来源通过 `LifeEvidence` 绑定 kind/target/revision，梦、生成叙事 ID、角色自述及跨范围来源不能代替用户授权。宿主仍负责核验自然语言意图，程序不能仅从参数中证明“同意”的语义。

`resolve(... state="completed"|"cancelled", expected_version=..., evidence=..., reviewer=..., reason=..., request_id=...)` 终结安排。`revise` 可调整 open/proposed 内容，写新版本并回到 proposed，再次确认。`get`、`list_open`、`history` 和 `preview_due` 都复核来源。投递成功不完成承诺，必须有另外的完成依据。

来源被遗忘、纠错或失去版本支持时，当前投影转 invalidated，当前文本、触发文字和历史正文被屏蔽/清除；既有操作回执不会成为读取旧内容的旁路。修订之前的旧版本来源单独复核，忘记旧来源不会凭空撤销使用全新依据修订的候选。

## 事件触发

```python
memory.intentions.record_trigger(scope, "exam-ended", actual_confirmation_event_id,
                                 request_id="exam-ended-2026-10-27")
```

只接受可信宿主明确调用及真实用户来源，不从模型猜测或某个检索片段自动宣布事件发生。触发回执幂等，唤醒后的 `preview_due` 不消费事项、不发送消息。触发来源后来被撤销时，对应提醒同样失效。

## 用户允许的主动性

```python
policy = memory.intentions.configure_policy(
    scope, enabled=True, confirmation_refs=[actual_user_event_id],
    request_id="proactivity-001", expected_version=0,
    timezone="Asia/Shanghai", quiet_start="22:00", quiet_end="08:00",
    min_interval_seconds=86400, max_per_day=1,
)
```

默认无策略或策略被撤销时返回 disabled。启用需要用户确认来源；`pause` 停止提醒，`resume` 复用已配置的时段/频率但需要新的确认来源，`clear_policy` 清除授权并回到关闭的默认策略。策略变更使旧领取 token 失效。支持带夏令时的 IANA 时区；系统没有 tzdata 时明确拒绝该时区，不用固定偏移假装准确。UTC 始终可用。

安静时段按当地民用时间判断，可跨午夜；起止相同表示没有安静时段。间隔至少 60 秒、最多 30 天，每日额度 1..24。每日额度按用户时区的当地日期计算，按“已领取的不同 delivery_id”保留，防止两个窗口同时通过检查。失败重试复用当天保留的名额；发送失败后 5、10、20……秒退避，最多 300 秒，不会无间隔空转重发。

## 宿主领取与投递回执

```python
result = memory.intentions.claim_due(scope, worker_id="native-host", limit=1, lease_seconds=60)
for claim in result["claims"]:
    checked = memory.intentions.validate_claim(scope, claim["claim_token"])
    # 只有这里的可信宿主才决定是否、通过哪个已获授权渠道发送。
    # 实际渠道必须使用 claim["delivery_id"] 作为幂等键。
    memory.intentions.ack(scope, claim["claim_token"], delivered=True,
                           request_id="delivery-confirmation-001")
```

本服务只返回 `trusted_host_claim_only` 的 JSON 资料。`validate_claim` 是发送前复核，返回合法来源、文本、稳定 delivery_id 和租约；不得把领取结果当作永久可用的消息模板。承诺取消、策略暂停/改变、来源遗忘、进入安静时段或租约失效后均拒绝旧领取。超时由另一个 worker 重领后，旧 token 不能确认或覆盖新 worker。

`ack(delivered=False)` 退回 pending 并持久退避，之后可重试；同 token 同参数回执重复返回，不重复消费配额。回执只描述投递动作，不携带旧正文，也不把 intention 标 completed。

外部消息无法由 SQLite 原子撤回：发送成功、ack 前崩溃会导致重领，因此宿主/渠道必须按稳定 delivery_id 去重。取消只能阻止仍待发送的数据；已经被用户读到的消息不可能靠本库更新消失。本轮没有创建定时器、通知权限、第三方账户或任何外部通信。

## 持久化、迁移与清除

七张表全部含显式 scope 字段和 scope_key：intentions、intention_revisions、intention_operations、proactive_policies、proactive_deliveries、proactive_ack_receipts、intention_trigger_receipts。intention_revisions 与 proactive_deliveries 通过意向 ID 外键级联清理；范围清除仍必须同步删除其他授权/回执表。

主数据快照可携带承诺与状态、修订/投递回执；跨设备导入不是新的主动权限。主集成应将导入策略禁用，旧 claimed 改回 pending 并清空 token/lease，要求新的用户确认后再允许通知。正常同库重启不改变已经明确批准的策略，只通过持久租约恢复。

专项测试覆盖候选/确认边界、用户来源、跨范围隔离、时间/事件同时触发、修订与回溯、幂等、安静时段与 DST、跨连接额度、租约重领/旧 worker 拒绝、失败退避、暂停取消、遗忘历史屏蔽、原子回滚和不增加事实真实性。真实外部通知渠道验收由接入它的宿主负责。
