# MemoryDreaming

面向长期陪伴角色的持久记忆引擎：让同一个角色在切换窗口、重启进程后，还能想起**自己是谁、你们是什么关系、一起经历过什么、哪件事还没做完**。

当前状态：**v0.16，增加双时间修订回放、作用域物理清除、原子快照导入、关系图遍历、健康趋势快照、实体索引、跨进程候选缓存、摘要派生生命周期和可选变更提议审批**。模型默认只能读；可信宿主显式开启策略后，模型也只能创建纠错、遗忘或检查点提议，审批仍由宿主独立完成。DeepSeek Harness 插件直接接入用户消息、模型请求和回合结束事件，不注册 MCP 工具；上下文压缩事件会保存短 handoff。当前 326 个回归测试通过。跨机器打包、真实 Harness 热加载、完整外部备份清理和健康趋势告警仍待完成。

接入从 [宿主与 MCP 指南](docs/host-adapter.md) 开始，运行 [跨窗口示例](examples/host_lifecycle.py)，或安装 [`integrations/deepseek-harness`](integrations/deepseek-harness) 原生 Harness 插件。该插件直接接入用户消息、模型请求和回合结束生命周期，不注册 MCP 工具。

### DeepSeek Harness 接入

插件使用固定的会话绑定（`u1 / c1 / r1`）和独立数据库 `.memorydreaming/deepseek-harness.db`，因此切换聊天窗口不会改变记忆引擎的身份边界。安装或更新时应通过 Harness 的插件管理器安装 `integrations/deepseek-harness` 目录；插件由本地 Python 桥接进程调用 HostAdapter，在每轮请求前注入有限预算的恢复资料，并在回合结束后保存实际显示的助手回复。

当前 Harness 插件已接通逐轮用户事件、受预算限制的恢复检索和助手投递保存；后台整理队列、记忆抽取和核心候选审批仍需要独立宿主调度。

### 上下文占用

MemoryDreaming 不注册工具，因此不会增加工具 schema；只会按 `maxTokens`（默认 1200）注入当前回合所需的记忆资料。检索候选可在多个窗口/进程间共享，但共享表只存 ID 和版本指纹，不存正文，也不会自动注入上下文。Harness 仍会把 profile 中其他已启用 MCP 工具的 schema 一起放入请求；如果上下文仍然很大，应在上下文面板按命名空间检查并关闭不使用的 MCP server（例如 `mcp__rea__*`、ComfyUI 或 PhotoCraft）。

受控修改见 [核心候选与审批](docs/core-proposals.md)，可运行 [重启后审批示例](examples/core_proposal.py)。

后续开发以这三份规格为基线，均明确区分当前实现与待实现设计：

| 文档 | 内容 |
|---|---|
| [系统架构 v0.2](docs/architecture-v0.2.md) | 核心与四类记忆、作用域、生命周期、模块边界及演进顺序 |
| [算法规格 v0.2](docs/algorithms-v0.2.md) | 形成、路由、时间、融合排序、预算、纠错、遗忘和核心成长 |
| [LLM 接口协议 v1](docs/llm-interface-v1.md) | 宿主钩子、查询/建议工具、JSON Schema、结果/错误、模型适配与样例 |
| [实体索引](docs/entity-index.md) | 结构化实体投影、作用域隔离、增量更新与重建边界 |

早期提案与已核查开源参考保留在 [v0.1](docs/memory-engine-architecture-v0.1.md)。正确性修复见 [代码审查记录](docs/review-2026-10-26.md)，队列机制见 [可靠巩固任务](docs/reliable-consolidation.md)，后续顺序见 [待解决问题](docs/open-problems.md)。Python 实现查询与核心建议子集；MCP 保持只读，通用修改工具与 HTTP 服务仍未开放。

## 先跑起来

要求 Python 3.10 以上（开发用的是 3.13），**没有任何第三方依赖**，clone 下来就能跑。

```bash
python -m memorydreaming demo     # 建核心 → 记对话 → 抽取 → 去重 → 检索 → 纠错 → 删除
python -m memorydreaming eval     # 检索评估：Recall@5 / MRR / 无依据拒绝 / 主题索引
python -m unittest                # 跑全部测试（当前 326 项）
python -X utf8 examples/host_lifecycle.py --db .memorydreaming/host-demo.db
python -X utf8 examples/core_proposal.py
```

`demo` 会打印每一步在做什么，输出末尾会告诉你数据库文件在哪。想自己看数据，直接用 sqlite3 打开它。

命令行也支持分步操作：

```bash
python -m memorydreaming --db my.db init
python -m memorydreaming --db my.db observe --user u1 --character c1 --content "我叫林鸥，喜欢猫"
python -m memorydreaming --db my.db consolidate                      # 抽取，本批新任务留到下一批
python -m memorydreaming --db my.db consolidate                      # 去重
python -m memorydreaming --db my.db consolidate                      # 摘要（达到生成条件时）
python -m memorydreaming --db my.db jobs                             # 就绪、退避、运行、失败统计
python -m memorydreaming --db my.db retrieve --user u1 --character c1 "我喜欢什么"
python -m memorydreaming --db my.db bootstrap --user u1 --character c1
python -m memorydreaming --db my.db reindex --user u1 --character c1  # 重建向量与实体索引
python -m memorydreaming --db my.db forget mem_xxx                  # 抑制事实，返回精确回执
```

## 目录结构

```
memorydreaming/
├── models.py         领域模型：四类记忆、摘要卡、活动、核心快照
├── assertions.py     稳定断言表示、核心作用域键
├── clock.py          时钟与时区：UTC 存储、源时区保留、可替换的测试时钟
├── config.py         配置：48 小时窗口、候选预算、token 预算、衰减与向量阈值
├── errors.py         自定义异常：版本冲突、恢复失败……
├── runtime.py        对外门面：bootstrap / retrieve / observe / checkpoint / correct / forget
├── host.py           可信会话绑定、逐轮恢复、实际投递、只读工具
├── portability.py    便携快照导出、来源校验与导入 dry-run
├── references.py     随机会话引用、版本摘要、逐跳来源及遗忘检查
├── llm.py            工具参数校验、统一回应、TokenCounter
├── llm-tools.json    两个读工具和显式启用的核心建议工具定义
├── proposals.py      持久核心候选、来源/版本检查、宿主审批与回执
├── mcp.py            MCP stdio：工具与 bootstrap 资源
├── consolidation.py  异步整理：任务分发 + 抽取 + 去重合并 + 派生索引更新
├── worker.py         文件库任务心跳与可唤醒后台整理器
├── summarization.py  主题摘要：主题词分组、索引卡生成、卡片匹配
├── similarity.py     相似度：字符 n-gram Jaccard + 聚簇（去重共用）
├── extractors.py     示例规则抽取器（覆盖四类显式候选）
├── llm_extractor.py  DeepSeek JSON/Schema 抽取适配器
├── formation.py      四类记忆候选统一格式与来源校验
├── evaluation.py     检索评估：固定用例 + Recall/MRR + 观察项
├── cli.py            命令行入口
├── demo.py           可运行场景
├── storage/
│   ├── schema.sql    表结构（带注释，建议先读这个）
│   ├── database.py   连接、事务、初始化
│   ├── repository.py 各表的读写 SQL（含向量表、摘要表、实体索引和审计链）
│   └── __init__.py
└── retrieval/
    ├── timeexpr.py   中文时间表达 → 明确时间范围
    ├── planner.py    查询计划：四类相关度、时间、预算、是否延续当前交流
    ├── keyword.py    关键词检索（FTS5 trigram + LIKE 降级）+ n-gram 特征
    ├── embedding.py  嵌入模型协议 + 零依赖的哈希嵌入
    ├── vector.py     向量索引：增量维护 + 暴力余弦检索
    ├── decay.py      可用性衰减：半衰期随重要度变化，带下限
    ├── fusion.py     RRF 倒数排名融合
    ├── pipeline.py   检索主流程：摘要 → 四通道 → 融合 → 校验 → 预算
    ├── context.py    渲染上下文：主题索引、证据、缺口、记忆围栏
    └── __init__.py
tests/
├── helpers.py            测试脚手架：临时库 + 固定时钟
├── test_storage.py       幂等、去重键、中文检索、软删除
    ├── test_core_activity.py 核心版本、活动检查点、bootstrap
    ├── test_handoffs.py 压缩前 handoff、跨窗口恢复与幂等
    ├── test_retrieval_gate.py 低信息检索门控
├── test_core_proposals.py 候选隔离、审批幂等、来源/版本失效与迁移
├── test_retrieval.py     时间解析、路由、48 小时分级、预算
├── test_consolidation.py 抽取后续任务、去重合并
├── test_job_leases.py    租约、延迟重试、事务回滚、真实进程崩溃与迁移
├── test_fact_suppression.py  重提取防复活、片段隔离、核心字段与并发遗忘
├── test_host.py          绑定、跨窗口恢复、来源、原话屏蔽、预算与投递
├── test_mcp.py           MCP 往返、真实子进程、仓库外启动
├── test_summaries.py     主题卡生成、回源展开、失效
├── test_vector.py        嵌入性质、索引维护、通道接线
├── test_entities.py      实体提取、归一化、作用域隔离、快照与实体通道
├── test_decay_time.py    衰减、时间旅行、上下文围栏
├── test_correction.py    纠正与删除的传播
└── test_extraction.py    从事件到记忆的整理流程
```

## 代码导读（按这个顺序读）

新手最容易的迷失方式是「从入口点进去，一路跳到最深处」。建议反过来，自下而上：

| 顺序 | 读什么 | 读完你会明白 |
|---|---|---|
| 1 | `models.py` | 项目里到底有哪几种东西；为什么要区分「事件时间 / 记录时间」「模态 / 认识论状态」 |
| 2 | `storage/schema.sql` | 这些概念怎么落到表里；为什么四类记忆共用一张表 |
| 3 | `storage/repository.py` | 每条 SQL 在做什么；幂等、版本号、向量与摘要怎么存 |
| 4 | `runtime.py` | 宿主怎么用这套东西；一次 observe / retrieve 的完整动作 |
| 5 | `retrieval/pipeline.py` | 检索是怎么一步步收窄的，摘要和四通道怎么配合 |
| 6 | `retrieval/planner.py` + `timeexpr.py` | 「昨天」「上次」是怎么被翻译成时间范围的 |
| 7 | `consolidation.py` + `summarization.py` | 抽取、去重、摘要三者为什么是这个顺序 |
| 8 | `retrieval/decay.py` + `context.py` | 为什么「同样相关」的记忆还要再排一次序；上下文为什么要加围栏 |

`demo.py` 是这八步的活教材：一边读代码，一边对照它的输出。

接入模型抽取时不需要把供应商 SDK 放进引擎：实现一个接收 `prompt`（可选接收
`schema`）并返回 JSON 的 provider，传给 `LLMExtractor(provider)`，再作为
`MemoryRuntime(consolidation_extractor=...)` 的抽取器。模型输出仍会经过来源片段、
四类正文和作用域校验；文件库可用 `Settings(auto_consolidation=True)` 自动消费队列。

如果宿主拥有目标模型的 tokenizer，可以传入 `ExactTokenCounter` 获取真实预算；引擎默认使用标记为 `estimated` 的零依赖估算，不会自行下载供应商 tokenizer。

## 一次对话发生了什么

```
用户说话
  └─ observe(turn, scope, idempotency_key)
       ├─ 同一事务写入：原始事件 + 活动检查点时间 + 抽取任务      ← 幂等键防重复
       └─ 返回后，整理链异步执行
            ├─ extract：候选 → 校验 → 写入 memories（dedupe_key 防重）
            ├─ dedupe：同类型内 Jaccard ≥ 0.85 合并，副本标 superseded + part_of
            ├─ summarize：按主题词把 ≥3 条记忆聚成索引卡（只存概述 + 来源 ID）
            └─ journal：事件、任务、检查点和 handoff 追加到可校验哈希链（只存元数据指纹）

用户提问
  └─ retrieve(question, scope, as_of?)
       ├─ planner：四类相关度、时间范围、每类候选预算、是否延续当前交流
       ├─ 摘要阶段（仅在「宽泛/久远」问题）：先给索引卡，再回源展开
       ├─ 五个通道取候选：
       │    最近 48 小时（仅当问题指向当前对话）
       │    关键词（FTS5 trigram + LIKE）
       │    向量（余弦相似度）
       │    实体（结构化 subject / object / participants 等字段）
       │    明确时间范围（仅当用户给了硬约束）
       ├─ RRF 融合 → 回表校验（撤回、越权、硬时间约束、被合并的副本、as_of 快照）
       ├─ 排序：融合分 → 可用性衰减 → 重要度
       ├─ 按 token 预算截断
       └─ 渲染成带围栏的上下文 + 检索轨迹 + 缺口说明
```

## 数据模型速览

四类记忆共用一张 `memories` 表，差异体现在 `memory_type` 和 `body_json`：

| 类型 | 正文结构 | 什么时候写 |
|---|---|---|
| 工作 working | `goal / open_loops / active_entities / checkpoint_note` | 当前目标、答应过但没做的事 |
| 情景 episodic | `what_happened / participants / context / outcome` | 一次可识别的事件 |
| 语义 semantic | `subject / predicate / obj / scope_note` | 用户明确说过的偏好、称呼、事实 |
| 程序 procedural | `trigger / preconditions / steps / exceptions` | 被验证过的相处规则 |

共同字段分四组，读代码时按这个顺序看最清楚：

1. **身份**：`memory_id / memory_type / scope`
2. **来源**：`source_event_ids / source_speaker / dedupe_key`
3. **效力**：`epistemic_status / modality / occurred_* / valid_* / status`
4. **权重**：`importance / retrieval_strength / access_count`

另有五类派生索引，以及持久整理队列。队列需要单独保证恢复，不能仅靠重建索引替代：

| 表 | 作用 | 同步方式 |
|---|---|---|
| `memories_fts` | 中文全文索引 | 触发器自动 |
| `memory_vectors` | 向量索引（float32 BLOB） | 写入时显式建立，可 `reindex` |
| `summaries` | 主题索引卡（概述 + 来源 ID） | 整理任务重建 |
| `entities` / `entity_mentions` | 结构化实体到记忆的反查索引 | 接受记忆时增量更新，也可按作用域重建 |
| `retrieval_cache` | 跨进程候选缓存（仅 ID 和版本指纹） | TTL、作用域代次或版本变化后失效 |
| `consolidation_jobs` | 异步任务队列 | — |

## 关键取舍（以及为什么）

**为什么是 Python + SQLite，不用 ORM？**
架构文档第 7 节允许 Python 或 TypeScript。选 Python 是因为没有构建步骤；不引 ORM 是因为新手最需要看清「数据到底是怎么存的」。每条 SQL 都在 `repository.py` 里，可以直接贴到 sqlite3 命令行验证。

**为什么四类记忆共用一张表？**
检索、纠错、删除的传播逻辑只需要写一遍。代价是 `body` 变成 JSON，失去了列级约束——但换来了「加一种记忆类型不用改表结构」。

**为什么中文检索是 trigram + LIKE，而不是分词器？**
SQLite 默认分词器会把一整句中文当一个词，查「海边」永远匹配不到「海边的约定」。FTS5 的 trigram 分词器能做子串匹配，但要求查询词至少三个字；两字词（橘猫、海边）就退回 `LIKE`。没有引入 jieba，是为了保持零依赖。以后要换分词器，只改 `retrieval/keyword.py`。

**48 小时到底限制了什么？**
只限制**默认展开粒度**，不限制能不能查到旧事。三天后问「第一次说去海边是什么时候」，仍然要能定位到那天的记录——测试 `test_old_memory_is_still_reachable_by_keywords` 就是在守这条线。

**「今天接着讲」里的今天，为什么不算过滤条件？**
因为它指的是说话时刻，不是要查的事件时间。planner 会把这类「向前看」的句子里的时间词降级为软提示。

**最近 48 小时的记忆，为什么不是「总会端上来」？**
架构文档把这条通道的用途限定为「继续当前交流」。如果无条件启用，「量子力学是什么」也会带上今天的聊天记录。所以只有出现「刚才 / 继续 / 我们聊」这类说法时才开（`plan.prefers_recent`）。

**硬时间约束为什么放在校验层，而不是各个通道里？**
用户说「昨天」就是强约束，任何通道（包括以后新增的向量通道）都不许绕过。放在 `_validate` 里只有一处实现，不会漏。

**去重为什么是「合并」而不是「删除」？**
删除会让证据凭空消失，合并只是把两份说法并成一条：列表字段取并集、来源事件合并、重要度取最大。副本标 `superseded` 并挂 `part_of` 链接，审计时还能查到原话。检索时用「superseded + part_of」两个条件一起跳过——`part_of` 本身还表示合法包含关系。

**新事实和旧事实冲突时，为什么是「关闭有效期」而不是「覆盖」？**
这里借鉴 Zep/Graphiti 的有效期与来源链：旧事实不删除，只把 `valid_to` 关掉，
新事实带 `supersedes` 链接。覆盖会丢掉「他以前叫林鸥」这个事实，
而陪伴角色恰恰需要解释「你以前是这么叫我的」。
触发条件是规则化的：单值谓词（名字/住处/生日…）换值，或同宾语的否定翻转（喜欢→不喜欢）。
「喜欢猫」和「喜欢狗」不算冲突；复杂语义矛盾仍需单独判断。当前 `memory_revisions` 支持记忆层的双时间回放，核心和摘要的完整历史仍在后续计划中。

**逻辑删除为什么还要查「有没有别的来源」？**
抄的是 Zep 的 `remove_episode` 规则：一条派生记忆由 A、B 两条证据支撑时，
删掉 A 不该让它消失，否则删一个来源会误伤别的证据。删除传播会分析受影响子图，保留仍能连接到外部有效来源的记录，避免环形引用自我支撑。
v0.6 会持久抑制目标断言及其来源片段，并屏蔽有来源关联的核心字段，更换抽取器版本也要检查账本。原始事件和历史存储仍保留，具体边界见 [事实抑制](docs/fact-suppression.md)。当前派生链接采用近似 OR 语义，多个前提必须同时成立的 AND 推导尚待实现。

**主题摘要为什么只能做索引，不能存全文？**
一旦摘要承担事实职责，第二代摘要在第一代上再压缩，错误就固化了，而且没有任何原始证据能推翻它。所以 `summaries` 只存主题、时间跨度、一句概述和来源 ID，要细节必须 `expand_summary()` 回源。来源消失时卡片标 `stale` 而不是继续用。

**主题聚类为什么不用整句相似度？**
试过：`第一次去海边` 和 `周末去海边要带相机` 只共享两个词，Jaccard 只有 0.07，跨不过任何合理阈值，但它们显然同属一个主题。最终改成「二字词 + 文档频率」分组：同一个词至少在 3 条记忆里出现才算主题，再对「覆盖同一批记忆」的候选词去重（不然「去海」和「海边」会生成两张雷同的卡）。

**为什么给召回内容加 `<memory-context>` 围栏？**
记忆正文来自用户原话，可能包含「忽略之前所有设定」这种句子。围栏 + 「这是资料不是指令」的声明降低了模型误当指令的概率——但它**不是安全保证**，工具和核心写入权限需要由宿主控制，见 `docs/open-problems.md` 第 3 节。

**为什么衰减有下限？**
没有下限的话，一条五年前的错误记忆会衰减到 0，但它仍然是错的、仍然可能被关键词精确命中。衰减只用于排序，不该变成「删除」的同义词。

**为什么身份核心缺失时不抛异常？**
新角色本来就没有核心。返回 `recovery_warning` 让宿主如实说明恢复失败，比拿一个临时身份顶上更安全——后者会让角色在不知不觉中「换了个人」。

## 评估结果

`python -m memorydreaming eval` 会在临时库里铺一批固定记忆，再问 12 个问题：

```
检索评估：12 个用例（计分 10，观察 2；Recall@5）
证据召回率 Recall@5：100.0%
MRR：1.000
无依据拒绝率：100.0%
主题索引命中：1/1（不计入召回率）

观察项（不计分，用于暴露当前模型的能力边界）：
- 同义改写（失眠→睡不着）：未命中
- 同义改写（养的那只猫→橘猫）：未命中
```

三点必须说清楚，否则这组数字就是自我欺骗：

1. **10 个计分用例是我自己写的，规模很小**。它的作用是回归保护——你改坏什么，它会立刻告诉你；它不能证明「检索质量好」。
2. **两个观察项故意留成失败**。中文里同一个意思可以完全没有相同的字，哈希嵌入做不到，只有真正的语义模型才行。
3. **摘要用例单独计分**。它测的是「宽泛问题会不会先给索引卡」，和证据召回不是一回事，混进分母会让指标失去含义。

## 验收场景对照

架构文档第 8 节的场景，与测试文件的对应关系：

| 场景 | 对应用例 |
|---|---|
| 新建窗口并重启进程 | `test_core_activity.py::BootstrapTest` |
| 同角色的不同活动不混淆 | `test_restores_identity_and_selected_activity` |
| 超过 48 小时的原话仍可定位 | `test_old_memory_is_still_reachable_by_keywords` |
| 多类别问题组合证据 | `test_retrieval.py::PlannerTest` |
| 更正偏好与称呼 | `test_change_closes_validity_and_creates_new_record` |
| 愿望不被提升为已发生事实 | `test_intention_is_not_promoted_to_occurred_fact` |
| 反复检索不提高真实性 | `test_retrieval_does_not_inflate_access_count` |
| 纠正/删除传播到派生记录 | `test_correction.py::CorrectionTest` / `ForgetTest` |
| 两窗口并发更新 | `test_stale_version_is_rejected` / `test_stale_checkpoint_is_rejected` |
| 没有足够证据时表达不确定 | `test_empty_result_reports_gap` |
| 同一件事被重复记下来 | `test_consolidation.py::DedupeTest` |
| 宽泛问题先给索引再回源 | `test_summaries.py::SummaryRetrievalTest` |
| 复盘「当时我们是怎么以为的」 | `test_decay_time.py::TimeTravelTest` |
| 新事实顶掉旧事实 | `test_conflict.py::ConflictResolutionTest` |
| 删一个来源不误伤派生事实 | `test_conflict.py::DeletionSupportTest` |
| worker 崩溃后恢复，旧 worker 不能重复提交 | `test_job_leases.py` |
| 检索质量可回归 | `python -m memorydreaming eval` |

## 已知限制与下一步

这一版是**骨架**，不是成品。刻意没做的部分：

- **抽取仍是有限的正则规则**，目前覆盖名字、称呼、约定和喜好。它证明了接口和流水线能跑通，不代表开放对话中的抽取质量。接模型时需实现 `MemoryExtractor`，并验证来源、模态和事实抑制规则。
- **嵌入是字面哈希**。同义改写召回不了（见评估的观察项）。换真实模型是替换一个类，但要重新标定 `vector_min_score` 并 `reindex`。
- **摘要抽取式生成**。读起来像清单，不是概述；主题词靠二字词频率，遇到指代会失效。
- **冲突检测只到规则级**。单值谓词换值和否定翻转能识别，复杂表达的冲突仍需验证（「养了猫」与「对猫过敏」本身可以同时成立，不能自动视作矛盾）；`as_of` 按「记录时间 + 有效期」回放，没有记录「何时得知失效」。
- **向量检索是暴力扫描**。本地几百上千条够用，十万条以上要换 ANN。
- **后台尚不会自动归纳人格成长**。`propose_core` 任务仍没有执行器；已支持基于实际用户消息提出核心字段候选并由宿主独立审批，见 [核心建议](docs/core-proposals.md)。
- **真实 Harness 往返仍待验收**。已有原生 Cordis 插件、生命周期桥接和进程内 HostAdapter；还需要在 Harness 主程序重启后验证多窗口、压缩、工具回合和异常退出等真实场景。
- **队列需要宿主持续轮询**。每次 `run_consolidation()` 会回收过期任务；库本身不会启动常驻进程。文件库可在耗时抽取时续租，内存库只有同步租约校验。向量计算仍在写事务内，接远程嵌入前需要把这一步移到事务外；跨设备同步尚未实现。

事实抑制的 API、迁移和边界见 [持久事实抑制](docs/fact-suppression.md)。完整问题清单见 **[docs/open-problems.md](docs/open-problems.md)**。schema v6 增加来源片段、抑制账本、内容代次及核心字段来源；v7 增加 host_deliveries；v8 纯新增 core_proposals，保存待审候选、来源版本与审批回执。迁移保留旧记忆与版本，不补造旧库没有的精确片段。

## 备注

- Windows 终端若出现 GBK 编码错误或中文乱码，用 `python -X utf8 -m memorydreaming demo`（其他命令同理）。
- Windows 上的 Python 默认不带 IANA 时区数据库，`clock.py` 对常用时区做了固定偏移兜底；想要完整的夏令时支持，`pip install tzdata`。
- `.memorydreaming/`、`*.db` 已在 `.gitignore` 中忽略，本地数据不会进版本库。
- 配置项见 `.env.example`，都可以用环境变量覆盖。
- 换嵌入模型或改维度后，旧向量会被自动跳过（维度不符），必须 `reindex` 一次。
