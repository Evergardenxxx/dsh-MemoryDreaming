# 核心候选与宿主审批（v0.8）

核心更新现在有独立的候选记录。模型可以建议调整称呼或角色名；建议持久化为 `pending_validation`，只有可信宿主核验实际用户意图并调用审批 API 后，才追加核心版本。默认不开启此工具，MCP 服务继续只开放两个读工具。运行示例：

```bash
python -X utf8 examples/core_proposal.py
```

示例使用临时库，展示“爸爸 → 建议老爸（未生效）→ 重启 → 宿主审批 → 下轮恢复老爸”。它不调用模型，审批只针对固定示例输入，不替代真实产品的授权逻辑。

## 接口

```python
from memorydreaming import CoreProposalPolicy, HostAdapter, SessionBinding

adapter = HostAdapter(memory, core_policy=CoreProposalPolicy(
    identity_fields=("名字",),
    relationship_fields=("称呼",),
    ttl_seconds=86400,
))
session = adapter.open_session(SessionBinding("u1", "c1", "r1", "window-1"))
turn = session.prepare_turn("user-message-1", "以后叫我老爸吧。")
target_ref = session.bootstrap()["data"]["relationship_ref"]

# 把 adapter.tools 转换为目标模型的工具格式。以下参数应来自模型的结构化调用。
result = session.call_tool("memory_core_propose", {
    "target_ref": target_ref,
    "fields": [{"name": "称呼", "value": "老爸", "evidence_refs": []}],
    "reason": "用户明确修改称呼",
    "max_tokens": 1200,
})
proposal_id = result["data"]["proposal_id"]
review = session.get_core_proposal(proposal_id)
```

`adapter.tools` 按配置返回两个读工具或三个工具，并将允许字段限定成 name 枚举；prepare_turn 的资料块给出 identity / relationship 各自的字段白名单。单独调用 `tool_catalog()` 默认仍是两个读工具。新增定义同步在[运行时目录](../memorydreaming/llm-tools.json)和[完整目标契约](contracts/llm-tools.json)。身份、审批者、用户事件 ID、实际核心版本均由宿主绑定或后端读取，不是模型参数。

工具只支持已经存在的核心。目标引用绑定具体版本与来源；首次建立身份仍由可信宿主调用 `apply_core_update()`。建议最多修改 12 个字段，字段名最多 100 字符，值为非空字符串且最多 1000 字符；不支持删除字段、任意 JSON 结构、检查点或事实纠错。字段必须在对应 identity / relationship 白名单内。模型字段白名单不代表自动批准权限。

`prepare_turn()` 必须先保存本轮用户消息。候选自动绑定这条真实消息，并将其作为每个修改字段的来源；不能用历史文字冒充本轮修改授权。每个字段可以再带最多 10 个用户事件/事实引用；摘要、核心本身、助手发言和推断记忆不能为核心更新背书。类型、来源和格式校验不证明语义正确，宿主仍须核验“用户是否要求这项修改、是否只是引用或假设”。

## 审批与回执

宿主确认意图和字段后，调用：

```python
receipt = session.approve_core_proposal(
    proposal_id,
    user_event_id=turn.user_event_id,
    reviewer="host:explicit-user-request",
    reason="宿主已核对本轮实际用户请求及字段",
)
```

需要拒绝时调用相同参数的 `reject_core_proposal()`。这些方法不注册为模型工具。用户已经明确表达改称呼等请求时，宿主可以按明确授权处理，无须设计成每次再问一次；存在歧义时才澄清。`reviewer` 只是可信宿主提供的审计标签，不是密码、签名或网络认证。不能把它或审批方法交给模型来授予执行权限。

候选、审批与核心使用同一个 SQLite 文件。每次审批在一个写事务内重新检查：绑定一致、授权事件 ID 一致、字段仍在白名单、未过期、目标核心版本与完整内容摘要一致、所有来源和遗忘代次一致。通过后合并最小补丁、保存逐字段来源，并原子提交核心与审批回执；任一步失败均回滚。

```text
pending_validation → applied       宿主批准且所有检查通过
                   → rejected      宿主拒绝
                   → invalidated   过期、版本/来源变化、遗忘或权限收窄
```

同一实际用户事件、目标版本、规范化字段与依据、理由组成幂等键；传输 request_id 或会话引用变化不会重复创建同一候选。默认有效 24 小时，重试不续期；每个绑定最多 64 个未过期待审项。审批重试必须保持原决定、审批者、理由与用户事件 ID，不重复追加版本。冲突不会自动重写到新版本，须读取新核心并重新提出建议。

`get_core_proposal()` 可跨进程、跨窗口检查同一绑定的候选，待审时返回字段和来源供宿主审核；终态只返回状态、版本与回执元数据，不再返回旧字段或理由。检查待审项也会执行失效检查，遇到变化即持久标记 invalidated。绑定不匹配与不存在统一拒绝，不泄露其他用户/关系/领域的候选。

工具回应符合现有 `data.kind=proposal`；批准/拒绝结果符合 `data.kind=change_receipt`。`state=applied` 只说明该操作曾经生效，不表示这些字段至今仍有效。非应用回执的 `remaining_work` 提供 PENDING_APPROVAL、REJECTED、EXPIRED、POLICY_CHANGED、VERSION_CONFLICT 或 STALE_REFERENCE。完整 JSON 仍遵守[统一结果 Schema](contracts/response.schema.json)。

## 兼容与边界

旧 `MemoryRuntime.propose_core_update()` 保留兼容，行为仍是可信本地直接应用；新增的明确名称为 `apply_core_update()`。二者都不是模型工具。修改工具返回 proposed 时不能告诉用户“已经改好”，只有实际 applied 回执才能确认生效。

核心字段继承请求事件和额外依据，后续事实抑制可沿这些来源清理核心。无关遗忘也可能改变 scope epoch，保守地使整份候选失效。来源属于其他关系或未授权活动时，核心字段仍按现有读取规则隐藏；身份版本可共享不代表获得来源所在关系的读取权限。

候选字段、理由与来源快照仍保留在 SQLite 审计记录中。这不是物理删除；完整擦除还需覆盖候选表、版本历史、WAL、备份及宿主历史。此版本没有自动从长期互动归纳人格变化，没有检查点建议执行器、通用修改/删除工具、网络审批认证或实际聊天应用集成。
