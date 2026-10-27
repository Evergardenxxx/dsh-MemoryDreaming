# 宿主适配与原生插件接入

宿主适配器实现 [接口协议 v1](llm-interface-v1.md)：Python 生命周期、受控引用、`memory_search` / `memory_read` 和可选变更提议。DeepSeek Harness 使用原生 Cordis 插件和本地 JSONL 桥接，不通过 MCP。当前 Python 包版本 0.20.0，数据库 schema 20；核心建议见 [核心候选指南](core-proposals.md)，事实修改与清除审批见 [纠错与变更审批](correction-approval.md)，后台抽取配置见 [DeepSeek 抽取指南](deepseek-extraction.md)。原生聊天桥接可启动独立后台整理器，自动消费事件队列。仓库另保留独立可选的 MCP 只读兼容入口。

## 先运行跨窗口示例

在仓库根目录执行：

```bash
python -X utf8 examples/host_lifecycle.py --db .memorydreaming/host-demo.db
```

示例建立“小梦”和父女角色称呼，记录睡前偏好及实际显示的回复；关闭第一个运行时后，新建运行时和窗口，再恢复身份、查询偏好、展开原话。它打印引擎的真实结果，不调用外部模型 API。省略 `--db` 使用临时数据库；指定路径会保留示例分区，重复运行不会覆盖已有核心。

## 宿主生命周期

```python
from memorydreaming import HostAdapter, MemoryRuntime, SessionBinding

with MemoryRuntime(db_path=".memorydreaming/host-demo.db") as memory:
    adapter = HostAdapter(memory)
    session = adapter.open_session(SessionBinding(
        user_id="u1", character_id="c1", relationship_id="r1",
        conversation_id="new-window-1", domain="real",
    ))
    print(session.opened)  # 协议 v1 bootstrap 回应；恢复不全会明确标记

    turn = session.prepare_turn("user-message-1", "还记得我睡前喜欢什么吗？")
    # turn.messages 是可供宿主转换为模型请求的消息片段。
    # 宿主保存自己的系统指令、历史、工具定义与输出预留，再调用目标模型。

    result = session.call_tool("memory_search", {
        "query": "睡前偏好", "activity_ref": None, "time_range": None,
        "detail": "evidence", "max_items": 4, "max_tokens": 1200,
    })
    print(result)
    session.close()
```

实际收到模型回复且确认显示后，宿主在同一运行时上下文中调用：

```python
receipt = session.after_delivery(
    "assistant-message-1", displayed_text,
    user_event_id=turn.user_event_id,
    delivery_state="final",
    used_references=used_refs,
)
```

`displayed_text` 与 `used_refs` 必须来自宿主实际投递结果。允许 `final` 和 `interrupted`；后者只保存已显示前缀。`delivery_unknown` 不保存为已发生的对话。每个投递记录是终态；重试保留相同 message_id、正文、用户轮次和状态，不把中断前缀改写为另一条完整消息。

`open_session` 立即读取核心和允许活动目录；`prepare_turn` 每次重新恢复，适用于新窗口、压缩后和核心已更新的情况。它先幂等保存用户事件，再检索；当前用户事件及引用它的候选/来源不会被当作历史依据。后台整理仍由宿主调用 `run_consolidation()` 或已有 worker 调度，没有隐含常驻进程。

`prepare_turn` 返回的前两条消息为固定使用说明和 JSON 资料块，最后一条为真实当前输入。核心文本保留在资料块，不提升成系统指令；JSON 中的角括号转义，不能靠正文闭合资料围栏。宿主必须把这些片段实际放入模型请求；仅创建 adapter 不会修改其他应用的聊天窗口。

宿主若能预判下一条查询，可调用 `session.prefetch("查询")` 或 JSONL bridge 的 `prefetch` 操作。它只预热候选，不返回记忆正文；下一次相同查询仍会重新校验作用域、遗忘状态和记录版本。SQLite 共享缓存保存候选 ID 与版本指纹，命中后为当前会话重新签发引用。缓存键是包含完整绑定、授权活动目录、选定/查询活动、检索参数与嵌入配置的 SHA-256 摘要，不保存查询原文。活动引用先解析成可信 ID，相同活动的不同会话句柄不影响共享。

schema 19 新增 `retrieval_state` 内容代次，由 SQLite 触发器监听主记录、来源、遗忘及实体/向量投影的增删改；即使其他进程或后台整理器新增记忆，也会让旧的非空及空结果缓存失效。代次和业务写入一起回滚；缓存写入、journal 和健康快照不推进代次。当前使用全库代次，其他作用域写入也会保守失效，后续可在规模测试后细化。

共享命中不会延长原到期时间。本地 LRU 默认 32 项，共享缓存默认上限 1024 项（`shared_retrieval_cache_capacity`，1..10000）；写入时清理过期和旧代次项，再淘汰最早写入的超额项。升级会清除 v17/v18 的旧查询缓存，不改主记忆。`cache_stats()` 是会话级计数；健康指标中的缓存数分为 `active/expired/invalidated`。后台调度与长期命中率聚合仍待完成。

### 上下文压缩前交接

宿主即将压缩当前上下文时调用：

```python
handoff = session.pre_compress(
    {
        "goal": "讲第三章",
        "open_loops": ["补上灯塔结尾"],
        "checkpoint": {"scene": "礁石旁"},
        "handoff_note": "下一窗口从第三章继续",
    },
    expected_version=activity_version,
    idempotency_key="compression-<host-request-id>",
)
```

活动字段会先按 `expected_version` 条件更新；版本冲突返回 `VERSION_CONFLICT`，宿主应重新 bootstrap 后合并。随后写入 schema v10 的短 handoff，下一窗口的 bootstrap 会带上同一活动的最新 briefing。重复发送同一幂等键只返回原交接，不会再次增加检查点版本。`checkpoint` 仍是工作资料，不能作为用户事实或核心字段的来源。

消息已落库后，即使恢复、检索、预算或截止时间失败，用户事件仍保留，可以使用相同 message_id 重试。消息幂等键由 conversation、speaker、message_id 构成；不是由 LLM 提供。助手事件、投递状态、来源快照和使用计数一起提交；重试不重复计数。不可用的使用引用不阻止保存实际回复，但会给出 warning、放弃热度增加，并阻止这条来源不明的回复被再次作为来源展开。

## 绑定与活动

SessionBinding 来自可信宿主配置，不是模型工具参数。user、character、relationship、domain 和 conversation 都必须明确；关系不能为 None。旧本地 Scope 的 None 关系有宽查询语义，不能直接透传到此接口。旧默认关系数据需要宿主明确归属，不自动猜测；身份核心仍可在同领域关系间共享。

允许活动通过 `allowed_activity_ids=(...)` 显式列出，最多 64 项；`selected_activity_id` 必须在目录内且活动确实属于当前用户、角色、关系、领域。默认不授权活动。活动 ID 与目录由宿主提供；LLM 只能使用 bootstrap 中的 `aref_...`。

搜索默认查询“共享事实（activity_id 为 NULL）+ 当前焦点活动”。没有焦点时只查询共享事实。模型可通过允许目录引用切换本次查询焦点，不改变宿主默认焦点。允许活动和本轮排除在关键词、时间、近期、摘要及向量候选 LIMIT 前应用，回表后再检查。其他活动再多，也不会先耗尽当前范围的候选位置。

按 ID 的旧 Python Runtime API 仍面向可信本地调用者；它们没有自动获得外部认证。此适配器也不是网络登录服务；MCP 每个进程使用启动配置中的固定绑定。

## 引用、来源与遗忘

`mref_...` / `aref_...` 是每会话随机句柄，注册表仅保留目标、摘要与过期时间，不缓存正文。默认有效期 30 分钟，容量 2048；关闭会话或新建窗口后需重新签发。引用不能由数据库主键转换得来。

读请求先验证整批句柄，再检查每条记录及其来源链；混有外国/伪造引用时整次失败。已知引用的正文、核心/活动版本、来源可见性或遗忘 epoch 改变时返回 STALE_REFERENCE；过期/未知句柄返回 NOT_FOUND_OR_FORBIDDEN。热度与访问次数不改变正文引用。

当前宿主层采用保守策略：任一来源缺失、越权、不可用或出现来源循环时，该候选不对外展示。它不改变底层近似 OR 传播，也没有完成通用 AND/OR 证据图。单次投影上限 512 节点、64 层；来源数组最多提供 20 个引用，尚无来源分页。

来源消息展开会检查已授权范围内的遗忘片段。可定位片段替换为 `[已抑制片段]`；定位不明则整条原话不展开。共享归纳引用活动内事件时也执行该检查。原始 SQLite 正文仍保留；这不是物理擦除。

投递记录保存实际使用的事件、记忆、摘要或核心引用及内容摘要。助手原话再次展开时沿该链复核；如果它曾复述后来被遗忘或修改的依据，会保守拒绝整条回复。宿主必须准确回传 used_references，遗漏的来源无法凭空恢复。没有来源的旧核心字段、活动检查点或任意自由文本仍不能可靠关联遗忘；已发给外部模型的历史也仍需宿主撤销。完整边界见 [事实抑制](fact-suppression.md)。

记忆/事件/摘要尚无完整修订历史：对外 record_version 使用 48 位内容指纹整数，属于不透明标识，不是递增时间线；引用内部比较完整摘要与 epoch。核心、活动沿用数据库的实际版本。不能把该机制解释为已实现双时间回放。

## 预算、错误与能力

`TokenCounter` 可从 HostAdapter 构造函数注入，要求 `mode` 为 estimated 或 exact，并提供 `count(text) -> int`。默认启发式估计，不能保证真实模型窗口上限。exact 只有在宿主接入目标模型实际计数器时才成立；测试中的字符计数器仅用于验证计量流程。

工具按最终序列化的业务 JSON 回应计量，包括字段、引用和预算元数据。模型可请求 128..4096，宿主默认最多 4096；未指定时使用 1200。超出时删除完整低优先资料，返回 partial、截断标记和缺口，不截断 JSON 或引文。连最小错误结构也放不下时抛出 CONTEXT_BUDGET_TOO_SMALL，MCP 在传输层返回错误。

prepare_turn 的 max_tokens 限制前两条记忆消息的序列化整体，包含固定说明、角色标签、围栏、核心和资料。先裁摘要/证据，再裁工作详情和活动目录；最小身份与关系放不下就失败。**它不覆盖宿主原有系统指令、当前输入、历史、工具外壳及输出预留**，这些仍由宿主计算整个模型请求预算。

deadline 是宿主传入的带时区 datetime，在执行前后检查；不能强行取消阻塞中的自定义嵌入服务。prepare_turn 后每轮默认最多 4 次工具调用，新轮重置。纯 MCP 无消息生命周期，无法判断用户轮次，不宣称已有每轮配额或自动恢复钩子。超时/存储失败转换为可重试的结构化错误；不向模型回显内部路径或异常正文。

默认能力清单开放两个读工具、occurred 时间轴及三种 detail。明确时间区间保持左闭右开，overview 也不能绕过日期。v0.8 可在 Python 宿主显式启用核心建议及独立审批；通用修改、完整遗忘、检查点提议及 recorded/valid 时间轴不开放。

## MCP 配置

```bash
python -m memorydreaming --db .memorydreaming/host-demo.db mcp --user u1 --character c1 --relationship r1
```

MCP 客户端应以子进程启动该命令，用 stdin/stdout 交换 UTF-8 JSON-RPC；stderr 留给诊断。实现 initialize、ping、tools/list、tools/call、resources/list、resources/read，支持 2024-11-05、2025-03-26、2025-06-18 协议版本协商。单条输入最多约一百万字符；没有 HTTP、订阅、资源模板、提示词目录或跨设备服务。

[mcp-config.json](../examples/mcp-config.json) 已按本仓库路径配置，读取上述示例库。它通过 [run_mcp.py](../examples/run_mcp.py) 从任意工作目录启动源码，无需先安装包；换机器时调整 Python 命令、绝对仓库路径、数据库和身份。已安装包的环境也可直接使用 `python -m memorydreaming`。每个客户端的配置入口可能不同，示例没有自动写入任何应用配置。

`memorydreaming://bootstrap` 资源提供身份、关系和活动目录。客户端能读取该资源，不代表每次开窗口都会自动读取。要保证第一条回复和压缩后恢复，聊天宿主必须接生命周期接口或提供相应 hook；单靠 MCP 工具不能保证模型记得调用。

## 验证与后续

回归覆盖跨运行时恢复、共享/活动联合检索、来源逐跳隔离、遗忘后原话屏蔽、内容引用失效、消息幂等与事务失败、预算和截止时间。MCP 测试包含实际子进程与从仓库外启动，确认 stdout 只有 JSON-RPC。实际回应另按协议 JSON Schema 验证。

尚未完成：原生 Harness 插件的真实主程序验收、真实模型供应商往返测试、可选语义模型、通用修改建议/执行、完整删除、完整证据组和双时间修订。核心候选/审批已在 v0.8 实现；SDK、MCP 服务和原生插件的存在都不表示所有宿主已经安装并启用。
