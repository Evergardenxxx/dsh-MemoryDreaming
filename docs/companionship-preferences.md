# 可纠正的场景陪伴偏好

本功能保存“用户希望怎样被陪伴”，例如平时可以直接给建议、难过时先倾听、庆祝时用简短回应。它是与人格核心分离的场景投影，不推断医学或心理诊断，不把模型自我评价当成用户事实。场景由可信宿主显式选择，模型不能凭自己猜到的情绪替用户确认。

## 参考的成熟机制

2026-10-27 实际读取了以下公开实现及固定提交内容，采用机制而非复制第三方代码：

| 来源 | 核对到的机制 | 本项目使用方式 |
|---|---|---|
| [LangMem README](https://github.com/langchain-ai/langmem/blob/48e3c11f5bb527282c7d5339c6a87a0b35abccfc/README.md) 与 [knowledge/extraction.py](https://github.com/langchain-ai/langmem/blob/48e3c11f5bb527282c7d5339c6a87a0b35abccfc/src/langmem/knowledge/extraction.py) | 区分对话 hot path 的记忆管理工具和后台 extract/consolidate/update。抽取契约把 preference 作为可独立描述的记忆内容，并要求提取重要偏好、关系与上下文。 | 把偏好提议、用户确认、场景选择和读取作为独立服务；普通对话不需要携带整份历史，睡眠或宿主可以提出候选，但不能自己批准。 |
| [Mem0 memory/main.py](https://github.com/mem0ai/mem0/blob/26cdefb46600c0fa5953c4b5ee5003c125539fc4/mem0/memory/main.py) | `history()` 提供修改历史，`_update_memory()` / 删除入口写入 ADD/UPDATE/DELETE 记录，保存旧值与新值；支持 namespace/身份范围下的记忆管理。 | 确认、纠正和撤销都产生持久版本，读取只用当前 head，撤销后不会静默回退旧偏好；原始依据失效时，历史检查也不再返回旧正文。 |

公开机制证明这些工程模式已经存在，不能据此声称 LangMem 或 Mem0 的所有模式都具备本项目的审批、遗忘屏障或精确场景语义。本服务使用自己的 SQLite 事务、LifeEvidence、原始事件权限和审计链。

## SDK 与确认流程

```python
from memorydreaming.companionship import CompanionshipService

service = CompanionshipService(memory)
candidate = service.propose(
    scope, "安慰方式", "先倾听，再询问是否需要建议", context="sad",
    evidence=[actual_user_event_id],
    reason="用户描述了希望的相处方式", idempotency_key="comfort-proposal-001",
)

# 以下入口只由已经独立核验用户意思的可信宿主执行。
receipt = service.confirm(
    scope, candidate["proposal_id"], user_event_id=actual_user_event_id,
    reviewer="trusted-host", reason="已核对用户明确确认",
    expected_version=candidate["expected_version"],
)

preferences = service.read_for_context(scope, "sad")
```

`propose` 返回 pending，不改变现有表达方式。证据须是当前可读取的真实事件或有效记忆，保存固定 kind/target/revision。角色实际说过的话可以成为待审建议的观察依据；`confirm` 还必须绑定同一完整 scope、说话者为 user 的真实确认事件。程序能校验事件与版本，不能认证自然语言的授权意图，独立核验仍由宿主负责。梦、核心自我背书和未知生成记录不能作为本服务依据。

`expected_version` 锁定该 scope/key/context 的当前 head；两个窗口基于同一版本提出不同候选，只能由当前版本匹配的一项生效。重复提议幂等；相同内容与来源不会因换理由/幂等键成为新候选。重复审批返回原终态回执，不再追加历史，改变原审批参数会失败。回执说明过去已发生的操作，不保证该偏好在后来的遗忘/撤销后仍然有效。

`correct` 是提出替换候选的兼容入口，仍需再确认；不会仅因方法名“纠正”而绕过审批。`feedback(approved=True/False)` 分别使用确认/拒绝流程。拒绝后同内容、同来源的建议返回既有 rejected 状态，不能不断提出施压；用户后来提供新的明确依据时，可以提出新候选，允许其改变想法。

## 场景与撤销

每个偏好以精确 `(user, character, relationship, activity, domain, key, context)` 维护当前版本。key/context 使用 NFKC 和 casefold 规范化，value 保留确认时的原值。读取某场景时先看 default，再由该场景的当前记录覆盖；存在该场景的撤销/失效 head 时，会阻止同 key 默认值偷偷补回来。

```python
service.revoke(
    scope, "安慰方式", context="sad", user_event_id=actual_user_event_id,
    reviewer="trusted-host", reason="用户撤回这条方式",
    expected_version=current_version, idempotency_key="revoke-comfort-001",
)
```

撤销产生新版本且当前 value 为空；不删除原始事件，也不自动永久遗忘其他相处记忆。历史记录可以解释过去的变化，普通 `read_for_context` 从不回退以前已被替代的版本。新候选的确认事件须属于精确 scope，不能以另一个关系、领域、活动或用户的事件授权。

## 来源撤销与持久化

`read_for_context`、`list_confirmed` 和 `history` 每次在同一快照重查原来源和确认事件。事件遗忘、内容改写、支持失效或被抑制的片段会使对应值不可读；最新值失效不会使旧值重新成为当前偏好。`history` 对失效记录返回 `available=false, value=None`。pending 候选过期或来源撤销后，检查接口也隐藏其 value/reason。

`invalidate_stale(scope=None)` 可接入纠错/遗忘最终事务，清理失效候选和当前/历史投影正文；即时读取保护不依赖后台清理。`companionship_proposals`、`companionship_preferences`、`companionship_history`、`companionship_receipts` 四表都保存显式 scope 字段，无额外跨表子项。完整/作用域快照及 scope 物理清除应同时处理四表，主运行时负责统一集成。逻辑遗忘不代表外部备份或已经发给模型的上下文擦除。

偏好是有界输入：value 最多 1000 字符、key/context 最多 80 字符，读取默认最多 12 项。pending 最多 64 项，候选 TTL 默认一天、最大七天，重复请求不会延长。每绑定最多 256 个场景 head，避免把无限自由文本变成常驻上下文。正式模型注入仍由已启用窗口和整体预算决定。

## 验证

15 项专项测试覆盖待审/真实用户确认、场景覆盖、撤销不回退、版本竞争、拒绝去重（含来源反序）、关系/用户/领域/活动隔离、确认来源遗忘、原文修订、候选过期、梦不能背书、幂等、事务失败回滚和反馈参数。全部通过。测试使用临时数据库与实际来源校验，不调用模型或向外发送消息。
