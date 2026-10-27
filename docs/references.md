# 参考来源与借鉴边界

以下为开发过程中核对过的架构、公开实现和可靠任务机制。此页集中导航，具体源码位置、固定提交和采用差异保留在专项文档中。项目名称不表示依赖、合作、背书或相同的质量保证。

## 记忆架构与早期设计

| 来源 | 借鉴内容 | 说明 |
|---|---|---|
| [CoALA](https://arxiv.org/abs/2309.02427) | 工作记忆与情景/语义/程序长期记忆、内部动作与决策循环 | 概念框架；核心身份、48小时策略和本地审批由本项目设计 |
| [cogmem-agent / pj-memory](https://github.com/akira-001/pj-memory) | 独立身份、会话初始化/收尾、混合检索和重要度衰减 | 名称映射及固定提交见[早期核查](memory-engine-architecture-v0.1.md) |
| [Celiums-memory](https://github.com/terrizoaguimor/celiums-memory) | SessionStart、有界恢复、来源链、时间与派生物生命周期 | main与dev成熟度不同，不把dev提案当成已验收实现 |
| [neuromemory-ai / Engram](https://github.com/tonitangpotato/engram-ai) | 多意图计划、RRF、相对时间落地与会话内工作集 | Core含义不同；本项目的身份核心与进程持久化另行实现 |
| [petertilsen/cognitive-memory](https://github.com/petertilsen/cognitive-memory) | 有界缓冲、持久检索、访问增强与简化巩固 | 不照搬进程内缓冲的跨窗口假设或混合时钟 |

准确的名称/包地址对应、许可证记录、固定源码位置及局限见[架构v0.1参考表](memory-engine-architecture-v0.1.md)。这些是机制参考，本项目没有把上述项目的源码打包到基础引擎中；未来引入代码或依赖须单独保留相应许可与归属。

## 长期角色与后台整理

| 来源 | 借鉴内容 | 本项目说明 |
|---|---|---|
| [Letta](https://github.com/letta-ai/letta) | 跨会话持久角色资料与状态 | [自传、连续性和私密档案](narrative-continuity.md) |
| [LangMem](https://github.com/langchain-ai/langmem) | hot path与background memory manager分工 | [场景偏好](companionship-preferences.md)、[睡眠设计](sleep-design-references.md) |
| [Mem0](https://github.com/mem0ai/mem0) | ADD/UPDATE/DELETE及修改历史 | [版本化偏好](companionship-preferences.md)、[受控成长](long-term-role.md#受控成长) |
| [Generative Agents](https://github.com/joonspk-research/generative_agents) | 反思对应evidence节点、计划与观测分开 | [自传来源](narrative-continuity.md)、[承诺](intentions-proactivity.md) |
| [Sleep-time Compute](https://arxiv.org/abs/2504.13171) | 在查询前计算有界中间资料 | [睡眠研究边界](sleep-design-references.md)；不证明虚构梦提高事实准确率 |

## 可靠任务与评估

[Celery](https://github.com/celery/celery)的幂等/确认机制、[APScheduler](https://github.com/agronholm/apscheduler)的运行并发/错过时点/合并策略用于设计本地队列契约；固定资料见[承诺与主动性](intentions-proactivity.md)。本项目不依赖这两个框架运行，也不由SQLite承诺外部渠道exactly-once。

[LongMemEval](https://github.com/xiaowu0162/LongMemEval)列为后续通用评估参考；工程回归不是LongMemEval成绩，中文合成标签与独立人工验收也不混为一谈。状态见[质量验收](quality-acceptance.md)。
