# MemoryDreaming

面向长期陪伴角色的持久记忆引擎：让同一个角色在切换窗口、重启进程后，还能想起**自己是谁、你们是什么关系、一起经历过什么、哪件事还没做完**。

当前状态：**v0.2 可运行骨架**。架构文档前两阶段的主要能力都已落地——身份与关系恢复、原始事件保存、四类记忆、持久工作检查点、混合检索、去重巩固、纠错与删除，外加一套检索评估。60 个单元测试全绿。设计提案见 [长期记忆引擎架构草案 v0.1](docs/memory-engine-architecture-v0.1.md)。

## 先跑起来

要求 Python 3.10 以上（开发用的是 3.13），**没有任何第三方依赖**，clone 下来就能跑。

```bash
python -m memorydreaming demo     # 完整场景：建核心 → 记对话 → 抽取 → 去重 → 检索 → 纠错 → 删除
python -m memorydreaming eval     # 检索评估：Recall@5 / MRR / 无依据拒绝率
python -m unittest                # 跑全部 60 个测试
```

`demo` 会打印每一步在做什么，输出末尾会告诉你数据库文件在哪。想自己看数据，直接用 sqlite3 打开它。

命令行也支持分步操作：

```bash
python -m memorydreaming --db my.db init
python -m memorydreaming --db my.db observe --user u1 --character c1 --content "我叫林鸥，喜欢猫"
python -m memorydreaming --db my.db consolidate                      # 抽取 + 去重
python -m memorydreaming --db my.db retrieve --user u1 --character c1 "我喜欢什么"
python -m memorydreaming --db my.db bootstrap --user u1 --character c1
python -m memorydreaming --db my.db reindex --user u1 --character c1  # 重建向量索引
```

## 目录结构

```
memorydreaming/
├── models.py         领域模型：四类记忆、活动、核心快照……整个项目的「共同语言」
├── clock.py          时钟与时区：UTC 存储、源时区保留、可替换的测试时钟
├── config.py         配置：48 小时窗口、候选预算、token 预算、向量阈值
├── errors.py         自定义异常：版本冲突、恢复失败……
├── runtime.py        对外门面：bootstrap / retrieve / observe / checkpoint / correct / forget
├── consolidation.py  异步整理：任务分发 + 抽取 + 去重合并
├── extractors.py     示例抽取器（基于规则，可替换成模型）
├── evaluation.py     检索评估：固定用例 + Recall/MRR + 观察项
├── cli.py            命令行入口
├── demo.py           可运行场景
├── storage/
│   ├── schema.sql    表结构（带注释，建议先读这个）
│   ├── database.py   连接、事务、初始化
│   ├── repository.py 各表的读写 SQL（含向量表）
│   └── __init__.py
└── retrieval/
    ├── timeexpr.py   中文时间表达 → 明确时间范围
    ├── planner.py    查询计划：四类相关度、时间、预算、是否延续当前交流
    ├── keyword.py    关键词检索（FTS5 trigram + LIKE 降级）+ n-gram 特征
    ├── embedding.py  嵌入模型协议 + 零依赖的哈希嵌入
    ├── vector.py     向量索引：增量维护 + 暴力余弦检索
    ├── fusion.py     RRF 倒数排名融合
    ├── pipeline.py   检索主流程：计划 → 四通道 → 融合 → 校验 → 预算
    ├── context.py    把结果渲染成模型能读的上下文
    └── __init__.py
tests/
├── helpers.py            测试脚手架：临时库 + 固定时钟
├── test_storage.py       幂等、去重键、中文检索、软删除
├── test_core_activity.py 核心版本、活动检查点、bootstrap
├── test_retrieval.py     时间解析、路由、48 小时分级、预算
├── test_consolidation.py 抽取后续任务、去重合并
├── test_vector.py        嵌入性质、索引维护、通道接线
├── test_correction.py    纠正与删除的传播
└── test_extraction.py    从事件到记忆的整理流程
```

## 代码导读（按这个顺序读）

新手最容易的迷失方式是「从入口点进去，一路跳到最深处」。建议反过来，自下而上：

| 顺序 | 读什么 | 读完你会明白 |
|---|---|---|
| 1 | `models.py` | 项目里到底有哪几种东西；为什么要区分「事件时间 / 记录时间」「模态 / 认识论状态」 |
| 2 | `storage/schema.sql` | 这些概念怎么落到表里；为什么四类记忆共用一张表 |
| 3 | `storage/repository.py` | 每条 SQL 在做什么；幂等、版本号、向量存取怎么实现 |
| 4 | `runtime.py` | 宿主怎么用这套东西；一次 observe / retrieve 的完整动作 |
| 5 | `retrieval/pipeline.py` | 检索是怎么一步步收窄的 |
| 6 | `retrieval/planner.py` + `timeexpr.py` | 「昨天」「上次」是怎么被翻译成时间范围的 |
| 7 | `consolidation.py` | 抽取和去重的边界在哪；为什么合并比删除更安全 |

`demo.py` 是这七步的活教材：一边读代码，一边对照它的输出。

## 一次对话发生了什么

```
用户说话
  └─ observe(turn, scope, idempotency_key)
       ├─ 同一事务写入：原始事件 + 活动检查点时间 + 抽取任务      ← 幂等键防重复
       └─ 返回后，整理任务异步执行
            ├─ extract：extractor.extract(event) → 记忆候选 → 校验 → 写入 memories
            │    └─ dedupe_key = 事件ID + 抽取器版本 + 候选键（重跑不会重复写）
            │    └─ 然后自动排一个 dedupe 任务
            └─ dedupe：同类型、同角色内比对字符二元组重合度 ≥ 0.85
                 └─ 保留「更重要、更早」的主记录，其余标 superseded + part_of 链接

用户提问
  └─ retrieve(question, scope)
       ├─ planner：四类相关度、时间范围、每类候选预算、是否在延续当前交流
       ├─ 四个通道取候选：
       │    最近 48 小时（仅当问题指向当前对话）
       │    关键词（FTS5 trigram + LIKE）
       │    向量（余弦相似度）
       │    明确时间范围（仅当用户给了硬约束）
       ├─ RRF 融合 → 回表校验（撤回、越权、硬时间约束、被合并的副本）
       ├─ 按 token 预算截断
       └─ 返回证据 + 检索轨迹 + 缺口说明
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

`importance` 表示「值得记住」，`retrieval_strength` 表示「容易想起」，`epistemic_status` 表示「凭什么相信」。三者不能互相替代——这是整套设计的地基。

另外两张派生表：`memories_fts`（FTS5 索引，触发器同步）和 `memory_vectors`（向量，`BLOB` 存 float32）。两者都能从 `memories` 重建。

## 关键取舍（以及为什么）

**为什么是 Python + SQLite，不用 ORM？**
架构文档第 7 节允许 Python 或 TypeScript。选 Python 是因为没有构建步骤；不引 ORM 是因为新手最需要看清「数据到底怎么存的」。每条 SQL 都在 `repository.py` 里，可以直接贴到 sqlite3 命令行验证。

**为什么四类记忆共用一张表？**
检索、纠错、删除的传播逻辑只需要写一遍。代价是 `body` 变成 JSON，失去了列级约束——但换来了「加一种记忆类型不用改表结构」。

**为什么中文检索是 trigram + LIKE，而不是分词器？**
SQLite 默认分词器会把一整句中文当一个词，查「海边」永远匹配不到「海边的约定」。FTS5 的 trigram 分词器能做子串匹配，但要求查询词至少三个字；两字词（橘猫、海边）就退回 `LIKE`。没有引入 jieba，是为了保持零依赖。以后要换分词器，只改 `retrieval/keyword.py`。

**48 小时到底限制了什么？**
只限制**默认展开粒度**，不限制能不能查到旧事。三天后问「第一次说去海边是什么时候」，仍然要能定位到那天的记录——测试 `test_old_memory_is_still_reachable_by_keywords` 就是在守这条线。

**「今天接着讲」里的今天，为什么不算过滤条件？**
因为它指的是说话时刻，不是要查的事件时间。planner 会把这类「向前看」的句子里的时间词降级为软提示。这是「事件时间 ≠ 记录时间」的另一种变体，也是规则引擎最容易搞错的地方。

**最近 48 小时的记忆，为什么不是「总会端上来」？**
架构文档把这条通道的用途限定为「继续当前交流」。如果无条件启用，「量子力学是什么」也会带上今天的聊天记录——那不是记得住，是什么都能扯上。所以只有出现「刚才 / 继续 / 我们聊」这类说法时才开（`plan.prefers_recent`）。

**硬时间约束为什么放在校验层，而不是各个通道里？**
用户说「昨天」就是强约束，任何通道（包括以后新增的向量通道）都不许绕过。放在 `_validate` 里只有一处实现，不会漏。

**去重为什么是「合并」而不是「删除」？**
删除会让证据凭空消失，合并只是把两份说法并成一条：列表字段取并集、来源事件合并、重要度取最大。副本标 `superseded` 并挂 `part_of` 链接，审计时还能查到「它原本是这么说的」。检索时会跳过这类副本——用「superseded + part_of」两个条件一起判断，因为 `part_of` 本身还表示合法包含关系。

**向量为什么先用哈希嵌入？**
零依赖、可离线、结果可复现，能先把「存储 → 增量更新 → 融合 → 评估 → 重建」这条管道修通。它的能力边界很清楚：只认字面重合，不懂同义改写。换成真实模型只需实现 `Embedder` 协议（`name` / `dim` / `embed`）并从构造函数传进来，其余代码不动。

**为什么 retrieve 不自动增加访问次数？**
否则一条错误记忆会因为被反复检索而越排越前。只有宿主确认「这条真被用上了」才调 `mark_used()`。

**为什么身份核心缺失时不抛异常？**
新角色本来就没有核心。返回 `recovery_warning` 让宿主如实说明恢复失败，比拿一个临时身份顶上更安全——后者会让角色在不知不觉中「换了个人」。

## 评估结果

`python -m memorydreaming eval` 会在临时库里铺一批固定记忆，再问 11 个问题：

```
检索评估：11 个用例（计分 9，观察 2；Recall@5）
证据召回率 Recall@5：100.0%
MRR：1.000
无依据拒绝率：100.0%

观察项（不计分，用于暴露当前模型的能力边界）：
- 同义改写（失眠→睡不着）：未命中
- 同义改写（养的那只猫→橘猫）：未命中
```

两点必须说清楚，否则这组数字就是自我欺骗：

1. **9 个计分用例是我自己写的，规模很小**。它的作用是回归保护——你改坏什么，它会立刻告诉你；它不能证明「检索质量好」。
2. **两个观察项故意留成失败**。中文里同一个意思可以完全没有相同的字，哈希嵌入做不到，只有真正的语义模型才行。留着它们，比刷一个 100% 更有用。

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
| 检索质量可回归 | `python -m memorydreaming eval` |

## 已知限制与下一步

这一版是**骨架**，不是成品。刻意没做的部分：

- **抽取只有三条正则规则**（`extractors.py`）。它证明了接口和流水线能跑通，不代表抽取质量。接模型时只需新写一个实现 `MemoryExtractor` 的类。
- **嵌入是字面哈希**。同义改写召回不了（见评估的观察项）。换真实模型是替换一个类，但要重新标定 `vector_min_score` 并 `reindex`。
- **向量检索是暴力扫描**。本地几百上千条够用，十万条以上要换 ANN（sqlite-vec / faiss），只改 `vector.py` 的 search。
- **巩固只做了去重**。`summarize`（主题摘要）和 `propose_core`（核心成长候选）还是任务表里的类型，没有执行器——它们都需要归纳质量，没有模型时实现出来只是假动作。
- **没有宿主适配层**。真正接进聊天客户端需要会话/请求钩子（架构文档第 2 节提到，仅暴露工具不能保证第一轮回复前完成恢复）。
- **中文分词是启发式的**。会切出「边那」这种无意义片段，靠命中词数排序压噪。

建议的下一步顺序：① 接一个真实嵌入模型，把观察项变成计分项 → ② 接真实抽取器并扩充评估集 → ③ 写 summarize 执行器 → ④ 做宿主适配（MCP 或自有客户端钩子）。

## 备注

- Windows 上的 Python 默认不带 IANA 时区数据库，`clock.py` 对常用时区做了固定偏移兜底；想要完整的夏令时支持，`pip install tzdata`。
- `.memorydreaming/`、`*.db` 已在 `.gitignore` 中忽略，本地数据不会进版本库。
- 配置项见 `.env.example`，都可以用环境变量覆盖。
- 换嵌入模型或改维度后，旧向量会被自动跳过（维度不符），必须 `reindex` 一次。
