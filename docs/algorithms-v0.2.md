# MemoryDreaming 算法规格 v0.2

基线：v0.5.0。标为“当前”的公式对应代码；标为“目标”的算法规定后续实现行为，不将设计当成已上线能力。模块关系见 [架构](architecture-v0.2.md)，JSON 交互见 [LLM 协议](llm-interface-v1.md)。

v0.6 已推进 G 节中的事实抑制与在途任务 epoch 校验，并实现核心作用域/字段来源；具体算法、迁移和边界见 [事实抑制实现](fact-suppression.md)。未标注完成的目标流程仍属于后续设计。

v0.7 增加可信绑定、受控引用、逐跳读取校验、原话片段屏蔽、MCP 只读工具及记忆消息计量，见 [宿主实现](host-adapter.md)。这些机制没有实现完整证据组、删除或供应商请求预算。

## A. 核心恢复与上下文组装

核心恢复使用确定性键读取，不运行向量搜索。目标算法：

```text
prepare_request(binding, actual_user_message, model_limits):
    verify_binding(binding)
    core = latest_accepted_core(binding.identity_key, binding.relationship_key)
    verify_field_evidence_and_revocation(core)
    if required_identity_missing(core): return RECOVERY_INCOMPLETE
    activities = list_visible_activities(binding)
    active = choose_explicit_or_unambiguous_activity(activities, actual_user_message)
    event = observe_actual_message_once(binding, actual_user_message)
    evidence = retrieve(actual_user_message, binding, exclude_event_id=event.id)
    request = pack(core, active, evidence, history, actual_user_message, model_limits)
    return request
```

不明确选择活动时返回目录及缺口；不能把多个检查点拼成一份进度。核心的最小字段集合由角色配置确定，例如角色名、用户指定的相处设定、称呼及关键边界。恢复出来的字段必须保留版本和来源，临时生成的自我描述不参与回写。

目标总预算：

```text
B_memory = W_context - T_instructions - T_tools - T_user - T_history
           - R_output - R_margin
T_core + T_working + T_evidence + T_structure <= B_memory
```

所有项使用目标模型对最终请求的计数；宿主可以先压缩/裁剪 history 再重算。先为最小核心和当前消息保留空间，再分工作集与证据预算。预算不足时先删除可展开摘要和重复证据，最后裁掉低排名内容；最小核心放不下则返回 CONTEXT_BUDGET_TOO_SMALL。保留证据 ID、有效性和来源，不把半个 JSON 或半条引文截断后当完整证据。

当前：核心字段级来源、宿主记忆消息整体预算、工具 JSON 计量和本轮事件排除已实现。v0.22 原生插件增加完整请求预览、工具步骤重算和发送前总预算检查，详见 [请求预算](request-budget.md)。默认启发式 `CJK字符数 + 其他字符数/4 + 1` 不构成目标模型 token 上界；请求级计数必须应用供应商投影、chat template 和实际 tokenizer，不能用字符串 TokenCounter 替代。

原生生命周期 v0.12 在发布恢复结果前增加 session/state/pending/revision/signal 复核；取消已发送的 bridge 请求会终止共享 worker，其他在途请求明确失败。投递后使用与生成取消独立的幂等请求保存，不从未提交的流式内容猜测事实。状态机及自动验收见 [生命周期工程](native-lifecycle.md)。

## B. 记忆形成与 LLM 抽取

宿主先保存实际消息，不让模型通过工具伪造 speaker、时间或对话 ID。抽取器只返回结构化候选，来源由后端补全并核验。

```text
extract(event):
    snapshot = (event.id, event.revision, scope.mutation_epoch)
    drafts = rule_extractor(event) or llm_extractor(event, extraction_schema)
    validate_schema(drafts)
    for draft in drafts:
        require draft.source_span matches actual text in event
        require body matches memory_type
        derive/verify speaker, modality, epistemic_status from source
        reject unsupported inference, quoted/hypothetical fact promotion
        normalize and check tombstones
    commit accepted candidates under job lease + snapshot checks
```

同一事件可以产生多个候选；未发现值得记的内容返回空列表。`我想养猫` 是 intention；`她说“我喜欢猫”` 是 quoted；模型说“我们去过海边”只证明模型说过这句话。用户明确角色设定允许作为 explicit_setting，推测必须标 inferred 并保留来源。跨句推理需要带证据组，首版抽取契约只接受当前单事件可定位的候选。

当前幂等键：

```text
dedupe_key = event_id + ':' + extractor_version + ':' + candidate_key
```

`candidate_key` 必须由规范化内容与来源位置稳定生成，不能是每次随机 UUID 或候选数组下标。目标引入独立稳定 `assertion_key`，关联语义相同候选和遗忘标记；提取器版本升级不会成为新增证据。语义规范化有歧义时保留候选或隔离审核，不能仅按词面相近自动合并。

模型输出重试规则：先按 JSON Schema 和语义校验；至多一次格式修复请求，仍不合格即记录任务失败并走队列退避。当前 `DeepSeekExtractor` 使用 DeepSeek 的 OpenAI 兼容 `chat/completions`、`response_format=json_object` 和 `DEEPSEEK_API_KEY`，底层解析器只接受候选，不接受 event_id、scope、speaker 等权限字段。模型置信度只代表生成器自评，不等于事实概率。没有 DeepSeek 凭据时继续规则基线，明确报告能力下降。

当前工程实现补充一层 `MemoryFormationPolicy`：在 `_build_memory` 之前对整批候选执行 fail-closed 校验。它要求四类正文分别满足 `working: goal/open_loops`、`episodic: what_happened`、`semantic: subject/predicate/obj`、`procedural: trigger/steps`，并校验候选键、importance、来源说话者和列表字段。整批有一条不合法时任务不提交任何候选，由租约重试；这条策略只保证结构和来源边界，不把模型自评置信度当成事实概率。

## C. 多标签路由与候选预算

四种记忆对应四个相关度 `s_t ∈ [0,1]`，每个回答“这一类是否可能有用”，不要求相加为 1。没有校准集之前称为相关度，不称为概率。

当前规则：每类从 0.2 开始，命中类型提示词增加 `min(0.6, 0.25 × 命中数)`，再增加时间/原话线索分并截断到 1。完全无线索时四类都设为 0.5。当前候选配额：

```text
b_t = 8,  if s_t >= 0.5
      2,  otherwise
```

8 和 2 来自配置。低分类别保留探查配额，避免硬路由漏掉混合问题。例如“上次答应的睡前故事，今天接着讲”同时激活工作进度、情景约定、语义偏好和程序习惯。

目标规划器输入实际查询、可见活动、时区与来源可用性，输出相同结构；LLM 规划失败回退规则。自适应连续分配可以后续评估，先不替换当前基线。目标允许最多两轮扩展：证据不足时扩大软时间范围或未充分检索的类型，仍不足则返回 gaps；明确硬时间范围不能擅自取消。

## D. 时间与 48 小时策略

时间字段分别表示：`recorded_at` 为入库；`occurred_start/end` 为事件发生；`valid_from/to` 为事实适用；目标修订日志的 `tx_from/to` 为系统何时接受该版本。所有确定时间保存为 UTC，时间区间使用左闭右开 `[start,end)`，显示和相对日期解析使用消息的本地时区。

| 输入 | 计划 | 不允许的行为 |
|---|---|---|
| 昨天说过的原话、某个明确日期 | exact_lookup，硬时间范围，回源 | 改查最近两天来补空缺 |
| 最近两天，继续当前交流 | detail，检查点与近期细节优先 | 所有普通问题都塞近期聊天 |
| 更久以前，主题宽泛 | summary_then_expand，再按需读来源 | 只剩摘要，永久丢掉精确信息 |
| 今天接着讲故事 | “今天”描述当前动作，可作软提示 | 把所有旧约定排除 |
| 今天提醒我查昨天的约定 | “昨天”约束事件时间 | 因出现“今天/继续”而清除硬约束 |
| 十年前搬家，今天才告诉模型 | 事件时间为十年前区间，记录时间为今天 | 用入库时间冒充搬家时间 |

当前：明确日期为 exact_lookup；无时间提示默认 detail；有久远/粗粒度时间提示时可能使用 summary_then_expand。并非所有宽泛问题已自动识别为摘要模式。当前时间解析较简单，复杂双时间查询尚不支持。

目标双时间过滤：

```text
fact_as_known(valid_at=v, knowledge_at=k):
    valid_from <= v < valid_to（空终点表示无穷）
    AND tx_from <= k < tx_to
```

例如 20 日获知偏好从 10 日改变，“15 日真实适用值”与“15 日系统当时知道什么”可能不同。v0.5 的 `as_of` 只按记录时间/有效期简化筛选；没有修订日志时，适配器不能承诺完整 knowledge_at 回放，遇到此类请求返回 UNSUPPORTED。

## E. 混合检索、验证与排序

```text
retrieve(query, trusted_scope, limits):
    plan = plan_query(query)
    candidates = summary_sources_if_allowed(plan)
               + recent_if_continuation(plan)
               + keyword_per_type(plan)
               + vector(plan)
               + explicit_event_time(plan)
    ranked = weighted_rrf(candidates)
    records = fetch_and_validate(ranked, trusted_scope)
    records = enforce_status_time_sources_revocation(records)
    return fit_budget(records, summaries, gaps, limits)
```

当前加权倒数排名融合：

```text
RRF(m) = Σ[c 命中 m] weight(c,m) / (60 + rank(c,m))
```

rank 从 1 开始。关键词权重 `0.5 + 0.5*s_t`；向量 `0.55 + 0.45*s_t`；近期 `0.6 + 0.4*s_t`；时间 1.0；摘要来源展开 0.85。数值对应当前实现，不是经过大规模优化的最优参数。

当前索引命中只提供候选 ID。回表后再检查作用域、撤回/删除、合并副本、时间边界与有效性。历史事实可返回，但标记历史值。当前 as_of 查询跳过可变的当前摘要，防止后更新摘要泄露未来信息。v0.6 已在检索、摘要展开、核心与 bootstrap 加入 tombstone 复核；source_revision 和完整投影代次检查待实现。

当前衰减：

```text
age_hours = max(0, now - (last_used_at or recorded_at))
H = 1440 * (1 + clamp(importance, 0, 1))
strength = max(0.3, min(1, retrieval_strength * 2 ** (-age_hours/H)))
```

按 RRF 得分降序，其次 strength，再次 importance 排序。衰减不能撤回事实，重复检索不能增加独立证据。`mark_used` 只在确实用于回复时记录使用；它不是用户确认真实性。

当前默认向量为字面哈希，不能承担同义理解；目标真实嵌入应保存模型、维度、前缀、归一化策略和版本。远程向量计算移到写事务外；失败时可退回关键词/时间，但结果须标为 partial 和 degraded。

目标查询单次有截止时间、候选数和 token 上限。截止后返回已经验证的资料，标 partial；所有通道不可用返回 UNAVAILABLE。返回空集合只说明未找到证据，不代表事情从未发生；不能用较高的排序分数编造 certainty。

## F. 去重、事实更新与摘要

当前去重先按完整 scope、类型、模态、说话者、认识论状态、有效期分组，语义记忆还要求三元组相同，再做字符二元组 Jaccard：

```text
J(a,b) = |shingles(a) ∩ shingles(b)| / |shingles(a) ∪ shingles(b)|
near_duplicate if J >= 0.85
```

正文列表与来源去重合并，副本标 superseded 并建立 part_of；主记录和向量一起更新。相似但客体不同、否定不同或活动不同不能合并。当前两两比较最坏为 O(n²)，大规模需要分桶/候选召回；没有误合并可逆日志前不降低阈值。

当前规则冲突仅覆盖同主体的单值谓词换值和同客体的直接否定。更新按事实生效时间排列；补录较早事件不能关闭较晚的当前事实。目标：

- change：当时正确，现在改变，关闭旧有效期并追加新版本。
- correction：原先就是错的，撤回旧版本并重算派生物。
- uncertain_conflict：保留有来源的冲突并返回缺口，不由 LLM 任意挑一条删除。

当前 dedupe 后活跃记忆达到 4 条才排 summarize；同主题至少 3 条才生成卡片。这两个阈值不是同一个概念。摘要保存来源 ID，源记录变化立即隐藏旧摘要。它是索引卡，不增加证据根；没有来源支持的摘要不得成为新事实的唯一依据。

## G. 遗忘与防复活（目标，下一实现阶段）

先明确操作语义，不能用一个 `deleted` 状态承诺所有内容都被擦除：

| 操作 | 目的 | 原始正文 | 后续再明确告知 |
|---|---|---|---|
| forget_fact | 抑制一项断言及旧来源的重提取 | 默认保留其他事实所在消息；不是原文擦除 | 允许用户明确重新授权，创建新代次 |
| forget_event | 清除一条原始消息及其支持贡献 | 清空/移除指定正文与可逆内容副本 | 只从新事件产生新的依据 |
| forget_scope | 清除绑定范围的活动、事实、核心及正文 | 按 scope 清除，旋转其 generation | 新建代次，不继承旧引用 |

用户只说“别记住我喜欢猫”，不能默认删掉同一条消息里的名字。若用户要求从全部正文中擦掉该事实，需定位所有片段；无法可靠分割时报告未完全擦除并请求明确范围。逻辑抑制和原文擦除必须在结果中分别报告。

forget_fact 抑制当前断言本身，即使还有其他旧来源支持，同一断言也不能靠那些旧来源再次出现；forget_event 只撤销该事件的支持贡献，其他独立来源仍可维持断言。两者的目标不同，传播算法须先检查目标抑制资格，再计算剩余来源。

### G1. 抑制标记

目标 tombstone 保存：作用域 generation、目标类别、稳定断言指纹、受影响 event/revision/span 定位、原因、创建时间和撤销状态。指纹可以用 scope 专用密钥做 HMAC，审计日志不再复制被删除正文；HMAC 不是匿名化证明。

规范化不能依赖 extractor.version。语义断言可使用规范主体、谓词、客体及模态生成键，但同义改写未必规范到同一键，因此还需来源片段定位。重新抽取时若候选与被抑制片段重叠，且无法可靠区分是否同一事实，保守隔离该候选；不要仅凭哈希没命中就自动写回。来源定位字段不能保存被擦除的可读副本。

### G2. 一次遗忘的事务

```text
forget(command, binding, idempotency_key):
    validate_actual_user_intent_and_targets(command, binding)
    BEGIN IMMEDIATE
        deduplicate command by (scope_generation, idempotency_key)
        bump scope.mutation_epoch
        create content-minimal tombstones
        retract target assertions / redact requested event content
        revoke affected running job tokens; cancel affected pending jobs
        mark dependent memories, core fields, summaries and vectors unavailable
        recompute support closure; keep only content supported by allowed roots
        invalidate handles/caches; enqueue safe rebuilds for surviving content
        persist receipt and precise erasure level
    COMMIT
    return receipt
```

v0.6 的 forget_fact 已原子写入抑制标记、递增精确 scope epoch、传播失效并清理核心字段；返回 logical_suppressed 回执。它会收集同断言的全部旧来源，并为后续匹配候选补充新来源；来源收集先于重叠检查，不依赖记录遍历顺序。尚无上述持久操作回执、内容擦除、generation 或 canceled 状态。事实抑制保留原事件，过时代次任务重新计算以保留同消息其他内容；未来擦除操作则需要取消任务。不能直接重置所有失败任务，也不能让旧内存结果绕过提交校验。

### G3. 在途抽取、缓存和恢复

```text
compute snapshot: event_revision, scope_generation, mutation_epoch
on commit under valid job lease:
    require event still exists and is not erased
    require generation/revision/epoch still match
    re-check every candidate against current tombstones
    on mismatch: discard old output; replan from surviving current sources
```

在整个 scope 上递增 epoch 是首版保守方案，会使无关在途任务也重新计算；先保证正确性，后续再按来源缩小影响范围。read/检索/核心恢复都检查撤销状态；不能只阻止新写入却继续暴露旧摘要。

对话窗口里已经发给模型的旧内容也属于宿主缓存。遗忘回执必须通知宿主丢弃相关注入块并重建下一次模型请求；若目标供应商缓存/远端历史无法同步清除，返回明确未完成范围。向量重建不以被删除原文为输入。

备份、SQLite WAL、空闲页和供应商数据保留不因一条 UPDATE 自动擦除。区分 logical_suppressed、active_store_purged、backup_pending 等回执。恢复旧备份必须先应用最新删除账本并校验 generation；缺少账本时阻止该范围上线，不能宣称旧备份不会复活。物理清理单独维护，不把正常事务中的 VACUUM 当通用方案。

### G4. AND / OR 来源传播

受控重新学习已在 v0.23 实现，详见 [重新学习](relearning.md)。v0.24 实现以下类型化支持组及最小不动点求解，接口与边界见 [AND/OR 证据组](evidence-groups.md)。

将每项派生记忆的支持表示为多个组：

```text
supported(x) = OR[group g of x] (AND[parent p in g] supported(p))
```

例如两段分别独立陈述同一偏好的消息是两个单成员组；“用户有空”与“地点已开放”共同推出出行建议属于同一 AND 组。空证据组不能让 inferred 断言自动成立。

计算从未撤销的事件或已授权明确设定等根开始，做最小不动点：

```text
live = verified_surviving_roots
queue = live
while queue nonempty:
    root = pop(queue)
    update unsatisfied-parent counts for groups depending on root
    for every newly satisfied group:
        if child eligible and child not in live:
            add child to live and queue
invalidate affected accepted records outside live
```

单次计数求解为 O(V+E)，循环依赖没有外部有效支持时不会凭互相引用存活。v0.24 管理刷新按精确 scope 处理存量派生记录及其祖先闭包；尚无增量后台求解。事件、记忆、核心版本引用均锁定内容；旧 memory→memory 扁平派生链接保持 OR 兼容，新组内 AND、组间 OR。默认 4096 节点/16384 边上限，超限明确标记不完整并拒绝读取，不能据此批量撤回全部数据。

## H. 受控核心成长（候选与审批已实现，自动归纳仍为目标）

```text
evidence → core_change_candidate → policy/explicit feedback → accepted revision
```

用户明确称呼/设定可在字段白名单内经可信宿主应用；从长期互动推测的个性变化先留候选，不因反复访问而自动成为核心。字段逐项引用证据版本，写入使用 expected_version 条件更新，冲突后重新读取再判断。删除字段的依据时同步撤回或由其他有效来源重建该字段。

v0.8 增加持久 core_proposals 和可选 memory_core_propose。算法如下：

```text
propose(trusted_current_event, core_reference, field_patches):
    require current event is a saved user message in this conversation
    verify core reference, field allowlist and user evidence
    snapshot core version/revision and every evidence revision/epoch
    deduplicate(scope, current event, target snapshot, normalized patches, reason)
    persist pending_validation with fixed expiry; do not mutate core

approve(trusted_host, proposal_id, actual_user_event_id):
    begin write transaction
    require binding and authorization event match
    recheck allowlist, expiry, core snapshot and evidence snapshots
    on mismatch: persist invalidated receipt; do not mutate core
    otherwise: merge patch under expected_version, append core and applied receipt atomically
```

默认关闭建议工具；宿主启用后在工具 Schema 和记忆资料块提供字段白名单。模型不提供审批身份或实际用户事件 ID。幂等重试不延长有效期，不重复追加版本；默认 24 小时、每绑定最多 64 个未过期待审项。具体边界见 [核心候选说明](core-proposals.md)。

原 `propose_core_update` 保留为可信本地直写兼容入口，明确名称为 `apply_core_update`，均不注册为模型工具。自动从多轮互动归纳人格候选未实现；核心中的关系角色不能被推导成未经用户陈述的现实生物关系。

## I. 可靠队列（当前）

原子领取生成独立 token，设置租约并增加尝试次数；运行前逐条领取，不预占整批。模型抽取在事务外，文件库独立连接心跳续租；结果、后续任务、完成确认同一事务。

```text
retry_delay = min(retry_max_seconds, retry_base_seconds * 2 ** (attempts-1))
owned = status==running AND claim_token matches AND lease_until > now
```

到期按上限回收为 pending 或 failed，旧 worker 的所有写入回滚。当前指数实现限制指数避免溢出，默认租约 120 秒，最多尝试 3 次，失败退避 5 秒起、最多 300 秒。完整说明见 [队列设计](reliable-consolidation.md)。目标再加入 G3 的内容代次校验，租约本身不能解决遗忘后复活。

文件数据库运行时可选启动后台整理器：它以独立连接周期等待或被 `observe()` 唤醒，每批领取有限任务，复用同一套租约、心跳和原子提交；手工调用与后台调用可以并发，重复领取由 claim token 互斥。关闭时先停止 worker 再关闭宿主连接。`:memory:` 数据库不启动后台线程，测试和一次性导入继续使用显式 `run_consolidation()`。

## J. 上下文压缩前交接（v0.10，已实现）

上下文窗口即将压缩时，宿主不能依赖模型自己总结，也不能把完整历史重新注入下一窗口。调用 `pre_compress(state, expected_version)`：

```text
if idempotency_key already exists:
    return the original handoff; do not update the activity again
if selected activity and state contains checkpoint fields:
    require expected_version
    UPDATE activity ... WHERE checkpoint_version = expected_version
    on zero rows: VERSION_CONFLICT and rebootstrap
briefing = {title, goal, open_loops, active_entities, checkpoint,
            handoff_note?, next_action?, status}
INSERT session_handoffs with scope + conversation + source_event + activity_version
supersede the previous active handoff for this activity
return only the short briefing under a separate result budget
```

`session_handoffs` 是工作状态的派生投影，不是第五类记忆，也不能为语义事实或核心字段提供来源。`source_event_id` 只帮助宿主追踪这次交接对应的当前轮次；读取 handoff 时仍要按绑定 scope 过滤。活动版本号和幂等键共同防止两个窗口互相覆盖以及网络重试重复推进。新窗口 bootstrap 读取最新 handoff，仍需再次按当前活动和作用域恢复核心；handoff 缺失不应生成替代人格。

## K. 从成熟项目吸收的后续算法（待实现）

这些机制已纳入待办，但不改变当前证据边界：

1. **检索门控与缓存**：规则判断低信息输入；命中门控时跳过搜索，仍保存事件。当前会话缓存只缓存 `(query, scope, mutation_epoch, current_event, source_revision)` 对应的受控引用，TTL 到期、遗忘代次变化、原记录 revision 变化或引用过期都会回退到完整检索；`prefetch()` 只预热缓存，不向模型自动塞入正文。跨进程共享缓存仍待实现。
2. **召回强化与健康度**：被宿主确认实际使用的记忆可增加 retrieval strength/access count；它不能改变 epistemic status、来源数或事实置信度。健康报告分别统计 stale 摘要、缺来源候选、低置信推断和未覆盖 open loop。
3. **持久 journal 与派生物代次**：事件、检查点、整理任务、handoff 追加 hash 链；摘要、向量和图索引保存 source digest 与 algorithm version，源变化后依次经历 active → stale → withdrawn/rolled_back。
4. **可移植备份**：导出 manifest、作用域和源摘要；导入先 dry-run，应用遗忘账本和 generation 校验后才切换，输出残留报告。
5. **情绪/重要度与实体图**：arousal 只调保留与排序，实体索引只缩小候选集合；二者都不能绕过来源、时间和权限校验。

## L. 算法验收与对照

| 算法 | 正例 | 必须失败或降级的反例 |
|---|---|---|
| 恢复 | 新进程复用相同身份与关系 | 无有效核心不得创造替代人格 |
| 抽取 | 多事实分别有原文定位 | 引用、愿望、助手编造不升格事实 |
| 软路由 | 同一问题跨类型组合证据 | 低分类别不能被彻底封死 |
| 时间 | 数月前原话仍精确定位 | 硬日期不能被近期通道冲掉 |
| 融合 | 多通道相关证据优先 | 多通道命中不能称为独立证据 |
| 预算 | 最终请求可装入目标窗口 | 字符估计不能承诺精确 token |
| 遗忘 | 重启/重提取/重建后不复活 | 不能以逻辑删除冒充全部擦除 |
| 来源 | OR 保留独立支持，AND 缺一则失效 | 没有外部根的环不能自证 |
| 核心成长 | 明确反馈可追溯、版本冲突可见 | 无来源推测不能直接写核心 |
| 接口 | 无工具模型仍由宿主恢复基本记忆 | 未完成服务不能伪报已接入 |

路由、48 小时策略、摘要和衰减分别做消融；评估集与调参集分开。首版目标是错误行为可被拒绝、检索资料可被解释，而非让所有模块都必须调用一次 LLM。
