# 原生插件的完整请求预算

Python v0.22 / 原生插件 v0.11，数据库仍为 schema 21。继续使用 Cordis 插件和本地 JSONL bridge，没有注册模型工具。

## 问题与接口

旧版只计量 `prepare_turn.messages[:2]`，没有覆盖宿主指令、历史、工具 schema、当前输入和输出预留；后续工具步骤还会复用最初的资料。安装包源码确认：`agent/pre-step` 的 `decision.messages` 只是待追加消息，完整请求在后续 admission 才组装，不能把它当完整历史。

Harness 原生消息要求 `id`、`source` 和 typed content。Python SDK 返回的普通 system/user 消息不能直接作为 `user/message` 事件追加。v0.11 将固定资料说明与记忆围栏转换成一个 `source.kind=memorydreaming` 的原生 user 消息。只替换自己拥有的 surface 节点，不修改宿主系统提示或用户历史，也不每轮追加快照。旧内容仍在宿主事件日志中；这不是外部物理遗忘。

## 两层检查

1. `system-prompt/assemble` 保存本步骤接受的 sections、contexts、variables、tools。
2. `agent/pre-step` 识别可信 `source.kind=user` 的待追加输入，逐条用零预算 `prepare_turn` 幂等落库。真实 `user/message` surface 事件在随后 admission 才发生，不能只靠该事件触发首轮恢复。
3. `agent/request` 在 `next()` 返回路由后，通过已有的 `llm.resolveModelInfo` 查询窗口和默认输出 cap。用 `session.deriveMessages()`、待追加输入、渲染后的系统提示、运行上下文、工具和 toolHistory 构造预览。旧系统/运行快照和重试时已提交的输入可能被重复计算，属于有意多预留；admission 仍可能改变请求，预览不是最终保证。
4. 在预览中排除旧记忆，计算余量，调用 bridge 获取核心和检索资料。计量原生消息外壳，同时检查孤立记忆请求与整个预览请求。放不下时预算减半重试；最小身份/关系核心放不下则整块跳过，不截断字段冒充完整恢复。确定结果后一次替换或清空旧节点，相同内容不产生额外替换事件。
5. `llm/stream` 在调用 `next()` 前检查冻结后的完整 options，包括所有 messages、tools、toolHistory 和已解析的 `maxTokens`。最终窗口读取同一路由的 `session.requestContext()`。超限抛 `CONTEXT_WINDOW_EXCEEDED`，此监听器不调用供应商，宿主需要压缩历史或调整工具/输出预留。

Cordis 的 `next()` 不接受替换请求参数，最终 options 也已冻结。因此插件在最终边界只核验或拒绝，并重新检查实际保留的记忆消息是否超过记忆上限，超过时抛 `MEMORY_BUDGET_EXCEEDED`。仅检查携带本次 `agent/request` 原始 turn signal 的请求，同 session 的标题生成等辅助调用不受干预。每次工具步骤、重试和新轮次都重算并复核来源。完整历史/工具正文留在 Node 宿主，不通过 1 MiB 上限的 bridge 转发，模型也不会收到预算诊断。

首轮尚无系统节点时，插件把新记忆加入自己克隆的 pending decision 数组，让 admission 在系统消息之后提交；已有系统节点时才直接使用 surface 追加/替换。排队的多个用户输入在提交阶段不会重复重置最新轮次的投递关联。

## 算法与配置

```text
W = min(configured_window if present, provider_window if present)
R = max(actual_maxTokens or provider_default if known, configured_outputReserve if present)
B = count(complete_input) + R + requestOverhead + safetyMargin
headroom = max(0, W - B)
memory_limit = min(configured_memory_cap, headroom)
fit(complete_preview + native_memory) AND count(isolated_native_memory) <= memory_cap
dispatch only if count(final_complete_input) + R + requestOverhead + safetyMargin <= W
```

计数可能非加性，最终始终对整体重算，不依赖分项之和推断 tokenizer 结果。计数器接收供应商投影需要的完整输入字段；signal、sessionId 等执行句柄被排除。

| 配置 | 默认 / 规则 |
|---|---|
| `maxTokens` | 记忆上限 1200；128..4096，异常回退 1200；与供应商输出 cap 是不同配置 |
| `contextWindow` | 可省略，使用目标元数据；配置只能收紧已知窗口 |
| `outputReserve` | 可省略；配置是下限，不得小于最终实际输出 cap |
| `safetyMargin` | 512，非负安全整数 |
| `requestOverhead` | 64，非负安全整数，供应商额外模板开销 |
| `requestTokenCounter` | 可选可信对象，`{mode, count(request) -> nonnegative safe integer}`；不从模型参数接收 |

窗口或输出上限未知时不猜测：跳过注入，报告 `REQUEST_LIMITS_UNKNOWN`。没有注入记忆时允许原宿主继续发送，但不声称完整预算已验证。已经注入而最终窗口未知时，拒绝发送并报告 `REQUEST_BUDGET_UNAVAILABLE`。旧宿主缺少 assembly 或可替换 surface API 时报告 `REQUEST_PREVIEW_UNAVAILABLE`，保留真实用户事件。

`max_tokens=0` 是可信生命周期 API 的显式不注入模式，不开放给模型工具。`PreparedTurn` 返回真实 user_event_id、原用户输入、`counted_tokens=0`、`memory_status=recovery_status=skipped_budget`，不运行检索/恢复。bridge 延迟 bootstrap，避免大核心在落库前阻止消息保存。随后可用同一 message_id 增加预算重试或直接 `after_delivery`。正预算不足以装下最小核心时，SDK 仍抛 `CONTEXT_BUDGET_TOO_SMALL`，插件降为零预算，报告 `skipped_minimum_core`。

## 精度、诊断与边界

默认 `CJK字符数 + 其他字符数/4 + 1` 只是 **estimated**，不是目标 token 上界。它计算完整输入 JSON，但文件/图像投影、角色模板和工具投影可能改变真实输入。安全余量也不使估算变成精确计量。真实 DeepSeek tokenizer / 供应商模板尚未接入，不能宣称真实 200k 窗口已精确验收。

可信宿主可程序化传入请求计数器（以下 `hostTargetRequestTokenizer` 需要宿主实现，并非仓库已有 tokenizer）：

```js
apply(ctx, {
  requestTokenCounter: {
    mode: 'exact',
    count(request) {
      // 绑定 provider/model 的实际投影 + chat template + tokenizer。
      return hostTargetRequestTokenizer.countInput(request)
    },
  },
})
```

回调必须支持完整请求和仅带一个记忆消息的请求，不执行网络发送，且同步返回非负安全整数。Promise、布尔、负数或不安全整数明确失败，不静默降为 estimated。JSON 字符/字节计数不能称为目标请求精确计数。Python `ExactTokenCounter` 仍只对传入字符串负责，不能替代这个接口。

宿主可订阅 `memorydreaming/budget(sessionId, report)`：stage 为 preview/dispatch，包含 fits/exceeded/unavailable、计数模式、窗口、输入、输出预留、模板开销、余量及 memory_status；不含正文、核心字段、查询或工具定义。`included` 只指资料块已加入，不代表核心完整性或证据真实性，后者仍在资料块内。每轮至多记录一次恢复跳过原因。

本轮完整 505 项 unittest 通过；compileall、两份插件 JS 的 Node 语法检查和 diff check 通过。离线测试用模拟 Cordis admission 和真实 Python bridge，覆盖首轮顺序、多条排队输入及投递关联、surface 替换、工具/输出增长、最终窗口变化、零预算恢复、最小核心不足、缺失 API、辅助请求、请求计数器和原生外壳硬预算。测试计数器不代表 DeepSeek。

wheel 0.22.0 的 45 个源码/数据文件、npm 0.11.0 的 8 个文件逐项匹配仓库；在临时目录安装 wheel 并解包原生插件后，验证零预算保存 → 同事件恢复 → 投递保存，以及安装后的插件恢复身份 → 工具变大 → 清空记忆 → 拒绝超限请求。没有调用真实 API。CI 增加显式 Node 22 和两份 JS 的语法检查，远程 CI 尚未运行。

仍需真实主程序验收：hook 顺序/热更新、重启/压缩、模型切换、取消/异常退出，以及真实 tokenizer 与文件/图像/工具投影一致性。插件不会压缩宿主历史、卸载其他插件或删除已有工具定义。基础请求已接近 200k 时，只缩小记忆资料无法消除其他来源的占用。

v0.27 / 原生 v0.14 新增可配置的本地 DeepSeek tokenizer 适配，固定模型映射与资产 SHA-256，真实编码文本/工具投影，完整线上请求仍标 estimated；V3 资产不自动用于当前 V4.1。文本/工具投影已与安装版 serialize 对照，文件/图像/签名 replay 明确拒绝，见 [tokenizer 接入](tokenizer-integration.md)。安装版实际 Cordis/agent/compaction 的隔离验收另见 [运行时验收](installed-harness-acceptance.md)。
