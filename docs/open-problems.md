# 待解决问题与参考实现

更新于 2026-10-26。正确性修复见 [代码审查记录](review-2026-10-26.md)。以下记录进展与待完成工作；公开项目提供机制参考，最终需要在本项目验证。

后续实现遵循 [架构 v0.2](architecture-v0.2.md)、[算法规格](algorithms-v0.2.md) 和 [LLM 接口协议](llm-interface-v1.md)。这三份规格及 JSON 样例已编写，目标服务/算法仍按各节状态逐步实现。

## 1. 已完成（v0.5）：整理任务的崩溃恢复

已增加 claim_token、lease_until、available_at 和每任务 max_attempts。过期任务在下一次巩固调用时回收；文件库心跳使用独立连接续租。完成、失败、释放、续租都校验凭证和有效期，旧 worker 无法覆盖新持有者。

抽取在写事务外执行，记忆、来源变更、向量、后续任务与完成确认在一个事务内提交。失败指数退避，达到尝试上限后进入 failed。任务按批次冻结候选、执行前逐条领取，避免后排任务等待时耗尽租约。机制及边界见 [可靠巩固任务](reliable-consolidation.md)。

参考：[Procrastinate 心跳检测失联任务](https://github.com/procrastinate-org/procrastinate/blob/main/procrastinate/sql/queries.sql#L39)、[Celery visibility timeout](https://github.com/celery/celery/blob/main/docs/getting-started/backends-and-brokers/sqs.rst#L87)、[任务确认与幂等](https://github.com/celery/celery/blob/main/docs/userguide/tasks.rst#L21)。可以用 SQLite 实现这些机制，无须为租约迁移数据库。

已验证：强制结束写事务中的子进程后重启恢复；过期 worker 的记忆与后续任务全部回滚；索引/入队/确认失败不留下半成品；两连接竞争不重复领取；v4 旧任务迁移与重复打开。现在已增加可控后台整理器：文件库运行时可用独立 SQLite 连接自动领取 extract → dedupe → summarize，事件入队后可唤醒，关闭时停止；租约仍允许手工 worker 与后台 worker 并发。默认配置仍关闭，原生宿主桥接显式打开。尚需远程嵌入计算移出写事务，以及大规模性能测试。v0.6 又增加了遗忘代次检查，旧计算结果丢弃并重新排队。

后台整理器的边界是有意保守的：`:memory:` 库和没有提供抽取器的运行时不会启动线程；LLM 抽取器失败继续使用队列退避，不阻塞当前回答。生产环境还需要监控 `last_report/last_error`、失败任务告警和跨进程部署的单实例策略。

## 2. 部分完成（v0.6）：持久事实抑制；P0：完整内容删除

`forget_fact()` 已实现稳定断言 HMAC、来源片段抑制、核心字段来源清理、读取/索引复核，以及在途任务代次校验；`forget()` 调用同一入口。更换抽取器版本、重启和重建不能复活匹配断言或受抑制来源片段。旧副本与未来匹配断言的来源也会加入账本，防止后续换类型重提取。机制、回执和迁移见 [事实抑制](fact-suppression.md)。

这仍是 `logical_suppressed`：原始消息、SQLite 中的历史 payload 和备份保留。未知来源片段会保守隔离并在回执列出事件；跨独立事件的任意同义改写、无来源核心字段及活动检查点的任意自由文本尚不能可靠关联。尚无解除抑制的受控重新学习入口。

继续按三种操作推进：

- forget_fact：已实现本地持久抑制；待增加明确重新授权、持久操作回执及宿主旧上下文撤销。
- forget_event：清除事件正文，取消关联任务，更新来源列表、派生事实、摘要和核心投影。
- forget_scope：已实现本地 active store 物理清除、作用域/子活动代次递增、旧快照导入阻断和 journal 载荷重写；当前模型上下文与外部备份仍需单独处理。

不能为删除一个事实而默认删除整条消息中其他无关事实。来源引用需要类型化为 event、memory、core_version，并区分“任一独立来源足以支持”和“多个前提共同成立”。当前 derived_from 采用前一种语义，不能表达 AND 推导。

参考：[Graphiti remove_episode](https://github.com/getzep/graphiti/blob/main/graphiti_core/graphiti.py#L2174)会更新来源列表，处理无剩余来源的事实，并重建受影响摘要。需要借鉴完整失效链，只有父来源检查还不够。

已验收：匹配断言与已知片段在重提取、重建、重启和核心恢复后不复活；同消息其他已定位片段保留。后续验收还包括正文擦除、恢复备份前同步删除账本，以及 forget_event 仅撤销一个独立来源时保留仍有其他来源支持的事实。

## 3. 部分完成（v0.11）：宿主恢复与核心候选/审批；P1：产品接入验收

已实现 HostAdapter / SessionBinding、逐轮 prepare_turn、实际投递 after_delivery、受控引用和两个只读工具，并提供 MCP stdio 服务。DeepSeek Harness 另有原生 Cordis 插件：它监听用户消息、模型请求和回合结束事件，通过本地 JSONL 桥接调用同一 HostAdapter，且不注册 MCP 工具。共享/活动范围和本轮事件排除在候选 LIMIT 前生效；核心、记忆、摘要、原话及助手投递来源逐跳复核。入口、示例与边界见 [接入指南](host-adapter.md)。

本轮新增 schema v10 的持久 handoff：宿主可在上下文压缩前调用 `session.pre_compress(state, expected_version)`，先以活动检查点版本号保存目标、开放循环和当前场景，再写入幂等的短 briefing。新窗口 bootstrap 只恢复最新 briefing，不复制完整历史；重复调用同一幂等键不会再次推进检查点。JSONL bridge 已提供 `pre_compress` 操作，原生插件兼容 `agent/context-compressing` 与 `session/context-compressing` 生命周期事件。若宿主版本没有这些事件，仍可在自己的压缩回调中直接调用 bridge。当前插件还把注入预算硬限制在 128..4096 token，异常配置回退 1200，避免配置把整段历史重新塞回请求。

v0.8 已新增 [持久核心候选与宿主审批](core-proposals.md)：字段白名单、实际用户事件绑定、来源/版本快照、到期失效、幂等审批和原子回执。模型不能自行批准；MCP 保持只读。自动从长期互动归纳人格、检查点建议与通用修改/删除工具仍未完成。

接下来需要在真实 Harness 主程序重启后验收插件：新会话、每次模型请求、上下文压缩后、工具回合、取消和异常退出都要验证。当前插件桥接已覆盖首轮恢复和实际投递保存，但不能可靠判断模型实际使用了哪条不透明引用，所以投递回执暂不增加 `used_references` 热度。宿主还需判断实际用户意图，不能将模型填写的“用户要求”当作授权证据。

参考：[Celiums SessionStart hook](https://github.com/terrizoaguimor/celiums-memory/blob/4fcd62ced341536c3585deb8a31969e5c2145a82/packages/plugin-claude-code/src/hooks/session-start.mjs#L28)。宿主必须绑定可信 scope，不能把模型传入的用户 ID 直接当作授权。correct、checkpoint、expand_summary 等按 ID 操作的本地 API 目前仍依赖可信调用者。

从 cogmem-agent、Celiums-memory 和 cognitive-memory 纳入的后续待办（其中低信息检索门控、会话缓存、健康摘要和 durable journal 已实现）：

- `trivial query` retrieval gate：已实现对“你好/好的”等完整短语跳过检索，但不能跳过核心恢复或事件保存；后续用中文对话集校准误判边界。
- 会话内检索缓存和后台 prefetch：已实现受 TTL、scope mutation epoch、当前事件和引用 revision 约束的会话缓存；`session.prefetch()` / JSONL `prefetch` 只预热候选引用，不返回正文。真正的后台调度、跨进程缓存和指标仍待加入。
- recall reinforcement：把“被实际使用过”与事实真实性分开记录，只调整检索强度，不提高证据等级。
- memory health / summary health / confidence / gaps：`MemoryRuntime.memory_health()` 已输出按类型/状态计数、陈旧摘要、失败任务、无来源推断和开放循环；置信度校准、历史趋势与 UI 仍待加入，健康分数不能被模型当作事实概率。
- durable journal 与 hash chain：**已实现**。`journal_entries` 使用全局 sequence、prev_hash、entry_hash 追加；`MemoryRuntime.verify_journal()` 可检测篡改或断链，记录不复制正文。哈希链不能代替作用域权限和删除账本。
- derived artifact 的 `source_digest`、`algorithm_version` 与 `stale/withdrawn/rolled_back` 生命周期：**source_digest / algorithm_version / stale 已实现**；来源改变时摘要先隐藏，再由队列重建，`withdrawn/rolled_back` 仍待用于更复杂的派生物。
- portability export/import：**manifest、源摘要、作用域快照、dry-run 迁移和遗忘/generation 预检已实现**（见 [便携快照](portability.md)）；人工确认后的原子导入、残留报告和跨密钥迁移仍待完成。
- entity index、图关系遍历和更完整双时间 claim 查询；没有修订日志时，`knowledge_at` 请求必须返回不支持。
- 可选 arousal/importance 维度：只影响保留和召回排序，不能覆盖用户原话、来源和认识论状态。

v0.6 已迁移核心键：身份按用户/角色/领域、关系再按 relationship_id 独立版本化，并保存字段级来源。旧版未存核心领域，迁移只能按历史默认 real 归属，多世界旧库需要重新归属。`propose_core_update` 保留可信本地直写兼容，明确名称为 `apply_core_update`；模型工具只创建 core_proposals。身份来源属于其他关系或未授权活动时仍会隐藏字段，未实现跨关系来源授权。记忆围栏只帮助区分资料；工具和核心写权限仍由宿主控制。

已验收 SDK 的跨运行时恢复、缺失核心不被替换、真实子进程 MCP 查询、原话遗忘屏蔽和消息幂等，并用模拟 Cordis 生命周期验证原生插件桥接。下一步验收真实 Harness 第一条回复前和上下文压缩后的恢复；旧模型上下文撤销、真实 tokenizer 与网络服务认证仍需产品侧完成。

## 4. P1：中文语义检索与抽取质量

本轮修复了规则将引述、假设和复杂否定记成事实的若干错误，并增加了显式情景/程序候选及 `MemoryFormationPolicy` 四类最小格式校验。默认哈希向量只有字面重合能力，两个同义改写观察项仍失败；开放语言中的指代、复合否定、隐含习惯和多事件拆分仍需模型抽取与标注集。

建议提供可选真实嵌入与模型抽取适配器，保留零依赖基线。零依赖是当前实现选择，用户并未要求禁止可选依赖或 API。先建立中文标注集，再选择多语言模型；记录模型版本、维度、查询/文档前缀、归一化策略并重建索引。验证超时降级与关键词回退，重新校准阈值。

`DeepSeekExtractor` 已接入官方 OpenAI 兼容接口，具备超时、响应大小上限、错误脱敏和 JSON object 约束；仍需在真实账号上补充限流、一次格式修复和模型输出评估。候选可和相近旧记忆一起决策；没有充分依据时，不自动撤回旧事实。

参考：[Mem0 当前主线 additive 抽取](https://github.com/mem0ai/mem0/blob/main/mem0/memory/main.py#L918)、[cogmem-agent 混合检索](https://github.com/akira-001/pj-memory/blob/d0b2287e9a6fd2d19b0d77c88b48ea6169675250/src/cognitive_memory/search.py#L207)。实现随版本变化，不能将某一路径概括为所有模式都只增不改。

验收：同义改写、指代和多事实消息能召回；引用、愿望、虚构事件不会成为已发生的用户事实。分别衡量准确率和错误记忆率。

## 5. 已完成 P0：双时间修订与回放；P1：可逆派生生命周期

`memory_revisions` 已保存每次内容、效力和状态变化的 system time 快照；`knowledge_at` 与 `fact_at` 可分别查询系统当时知道的版本和事实有效状态，`as_of` 继续作为双轴兼容别名。历史查询绕过当前摘要、FTS 与向量投影，只使用修订快照。

例如 20 日获知某偏好从 10 日起改变，查询“15 日时系统认为怎样”和“15 日的实际状态”应允许不同结果。修改同一行 valid_to 无法完整表达。

派生摘要仍只支持 `stale`，`withdrawn/rolled_back` 以及核心版本的完整历史回放列入 P1；来源修订后先标 stale，再由可靠队列重算。

参考：[Graphiti 时间字段](https://github.com/getzep/graphiti/blob/main/graphiti_core/edges.py#L263)。有效期加 as_of 参数不能等同已经完成双时间数据库。养猫和对猫过敏可以同时成立，复杂事实的冲突也不能靠直觉直接判定。

## 6. P1：图关系、便携恢复、语义质量与预算计量

已增加 `MemoryRuntime.related_memories()` 的有界双向图遍历，沿 `supports/derived_from/supersedes/part_of` 关系回溯时仍检查作用域、遗忘账本和状态，不把关系边提升为事实。便携快照现在支持经过 dry-run 校验后的原子导入；不同遗忘密钥仍默认拒绝直接导入，避免旧账本复活。`record_memory_health()` / `memory_health_trend()` 持久化不含正文的健康计数，便于宿主做趋势告警。`change_proposals` 已支持可选的纠错、事实/事件/作用域遗忘和检查点提议；提议必须绑定本轮用户事件，审批时重新校验引用版本与作用域。

LLM `memory_search` 已能显式传入 `knowledge_at` / `fact_at`，宿主能力声明同步更新；`recorded` / `valid` 的 `time_range` 仍明确返回 unsupported，避免静默误查。

待完成：实体索引和更丰富的提议类型（结构化事实替换、核心以外的批量修改）、派生物 `withdrawn/rolled_back`、跨进程缓存与后台 prefetch 指标、真实中文 embedding/DeepSeek 抽取评估、精确 tokenizer 预算，以及真实 Harness 主程序重启/取消/异常退出验收。

## 7. P2：评估、人格成长和预算计量

277 个测试是工程回归；12 个固定检索用例中有 10 个计分、2 个观察项，不能证明长期人格稳定或中文语义检索质量。

建议建立独立中文陪伴评估集：跨会话恢复、称呼变化、共同经历、未完约定、旧事原话、反事实、删除后复活及多活动隔离。加入无依据负例，分别测证据召回、回答正确性、错误记忆、纠错生效、延迟和 token 成本。调规则用例与最终评估集分离。

[LongMemEval](https://github.com/xiaowu0162/LongMemEval)可补充通用能力评估，与中文集并行使用。对类别路由、48 小时分级、衰减和摘要做消融对照。核心成长先建立候选和用户反馈；访问次数不会增加真实性。

v0.7 已提供 TokenCounter，并对工具 JSON 和 prepare_turn 记忆消息整体计量，最小核心放不下时失败。默认仍为估计；宿主须接目标 tokenizer，并把原有指令、当前输入、历史、供应商外壳和输出预留纳入完整请求预算。
