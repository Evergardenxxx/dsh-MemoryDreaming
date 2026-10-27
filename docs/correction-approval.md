# 同步纠错与变更审批（v0.20）

目标：计算本地向量时释放 SQLite 写锁；完成计算后，只对仍然有效且获准的事实执行一次原子修改。接口仍通过可信宿主调用，DeepSeek Harness 继续使用原生 Cordis 插件及 JSONL bridge。没有新增模型工具、自动审批或额外上下文注入。

## 同步纠错算法

`MemoryRuntime.correct(memory_id, Correction(kind="change", ...))` 的签名保持不变，内部改为：

1. 复制请求，在 `read_snapshot()` 中检查旧记录、来源和遗忘标记，记录内容 generation 与作用域 epoch，生成替换记录。
2. 退出快照后调用 `prepare_memories()` 编码。其他连接可以写入，此时旧事实尚未结束有效期，新事实也不可见。
3. 短写事务检查运行时归属、计划完整性、generation、epoch 和来源；再原子写入旧事实有效期、新事实、supersedes 边、向量及实体投影。事实有效时间沿用请求时刻或显式 effective_from，系统 recorded_at 则在实际发布时赋值，避免慢编码让系统时间提前。

计划只在内存中存在，绑定运行时及旧/新记录完整内容，向量另绑定正文和模型 namespace。它不进入工具参数，也不是审批凭证。输入在编码期间被修改不会改变计划；计划自身被修改、跨运行时使用或再次发布都会被拒绝。

计算期间来源、记忆、索引或遗忘发生变化，SDK 抛 `SourceRevisionChanged`；调用者须重新读取并决定是否重试。当前采用全库 generation，无关作用域写入也可能引发保守冲突。模型、索引、链接或实体写入失败，旧事实和新事实一起回滚。

已删除或受抑制的目标不能通过纠错恢复；已撤回或结束有效期的旧事实不能继续作为替换入口。来源必须存在、未遗忘且属于目标范围。`kind="fix"` 不做向量计算，继续使用短事务撤回并使派生内容失效。

兼容边界：不得把同步 `correct(kind="change")` 包进外层写事务；预计算入口会拒绝这种用法，防止悄悄回退到持锁编码。内部审批执行器已改为分阶段调用。直接使用底层 `index_memory()` 且不提供 prepared 的旧行为仍保留。

## 审批算法

可信宿主仍使用 `session.approve_change_proposal()` / `reject_change_proposal()`；模型只能创建候选。

1. 短事务校验提议与请求事件绑定、终态、有效期、宿主策略及全部目标/依据 revision。
2. 结构化 `correct_fact` 在事务外准备替换及向量；其他操作不调用模型。
3. 重新开事务读取提议，完整重复上述检查；另一宿主已拒绝、已批准、策略收回、到期或来源变化都不能被旧计算覆盖。
4. 在同一 savepoint 中执行动作并持久保存回执，再提交外层事务。任何回执失败都回滚动作。

相同决定、请求事件、审批者和理由的成功重试直接返回原回执，不再次编码或创建记忆。冲突决定拒绝改写终态。临时模型不可用或只有全库 generation 变化时返回可重试 `UNAVAILABLE`，提议保持 pending；真正的来源失效、过期和版本冲突会终结候选。存储等非预期错误向上抛出并整体回滚，提议保留待重试，不再无条件永久标记 EXECUTION_FAILED。

`correct_fact` 一次只接受一个目标；批量替换仍待实现。自由文本缺少 replacement_body 时只撤回旧事实，回执为 partial，并明确结构化替换尚未完成。

## 统一回执与清除

所有变更回执符合 [response.schema.json](contracts/response.schema.json)：operation_id、proposal_id、operation、state、applied、effective_at、erasure_level、分类 affected 和 remaining_work。Schema 同时接受已有的 `change_` 和核心建议的 `proposal_` ID。事件正文擦除使用实际的 `event_redacted`，纠错/检查点使用 `not_applicable`。

affected 分别统计事件、记忆、核心逻辑字段及活动。结构化纠错计旧事实和新事实共两条；事件遗忘也统计保留独立来源但更新了来源列表的记忆；核心同一逻辑字段的历史版本去重。统计不含向量、摘要等派生投影。旧版整数 affected 无法可靠拆分，读取时返回零个已知分类计数、partial 及 `LEGACY_AFFECTED_COUNTS_UNAVAILABLE`，不伪造历史精确统计，也不修改旧回执存储。

`forget_scope` 会删除提议和请求事件，不能把成功回执继续挂在原提议行上。schema 20 新增 `change_purge_receipts`，清除后只保存随机操作/提议 ID、scope key、时间、无正文回执，以及审批参数的 HMAC。原始用户事件 ID、审批者和审批原话不写入该表。它不引用已清除事件，可跨进程查询和幂等重试；写回执失败会回滚整次清除。最小审计元数据像 purge marker 一样保留，不承诺擦除所有标识符。快照包含此表，旧 schema 19 及更早快照缺少此表仍可预检。

同时修复核心清除键的不一致：普通 scope 用五元组，核心 scope key 用四元组。现在分别匹配和删除核心版本及核心字段抑制记录；清除一个关系时保留其他关系、领域及共享身份。

## 验证与边界

新增 34 项回归，覆盖独立连接写入、修订/遗忘/作用域清除竞态、请求修改、计划复用、发布时系统时间、模型失败、索引/链接/实体/回执失败回滚、并发审批/拒绝、过期、策略收回、旧回执、清除后重启重试、核心隔离及 schema 19 升级和旧快照兼容。

全量 462 项 unittest、compileall 和原生插件 Node 语法检查通过。另用 JSON Schema 实际校验 8 份回执与 16 份工具响应；v0.20 wheel 的 44 个源码/数据文件与仓库一致，独立临时目录安装后完成 observe → consolidate → search → 审批纠错 → 作用域清除及幂等重试。验证只用哈希或可控测试向量，未运行真实语义模型。

尚无本地模型硬超时或强制取消；代次冲突也尚未细化到最小来源集。真实语义模型、真实 DeepSeek 质量及 Harness 主程序生命周期仍待验收。已投递上下文和外部备份未自动撤销，`full_forgetting=false`。
