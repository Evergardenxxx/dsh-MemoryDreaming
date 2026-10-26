# 待解决问题与参考实现

更新于 2026-10-26。正确性修复见 [代码审查记录](review-2026-10-26.md)。以下记录进展与待完成工作；公开项目提供机制参考，最终需要在本项目验证。

后续实现遵循 [架构 v0.2](architecture-v0.2.md)、[算法规格](algorithms-v0.2.md) 和 [LLM 接口协议](llm-interface-v1.md)。这三份规格及 JSON 样例已编写，目标服务/算法仍按各节状态逐步实现。

## 1. 已完成（v0.5）：整理任务的崩溃恢复

已增加 claim_token、lease_until、available_at 和每任务 max_attempts。过期任务在下一次巩固调用时回收；文件库心跳使用独立连接续租。完成、失败、释放、续租都校验凭证和有效期，旧 worker 无法覆盖新持有者。

抽取在写事务外执行，记忆、来源变更、向量、后续任务与完成确认在一个事务内提交。失败指数退避，达到尝试上限后进入 failed。任务按批次冻结候选、执行前逐条领取，避免后排任务等待时耗尽租约。机制及边界见 [可靠巩固任务](reliable-consolidation.md)。

参考：[Procrastinate 心跳检测失联任务](https://github.com/procrastinate-org/procrastinate/blob/main/procrastinate/sql/queries.sql#L39)、[Celery visibility timeout](https://github.com/celery/celery/blob/main/docs/getting-started/backends-and-brokers/sqs.rst#L87)、[任务确认与幂等](https://github.com/celery/celery/blob/main/docs/userguide/tasks.rst#L21)。可以用 SQLite 实现这些机制，无须为租约迁移数据库。

已验证：强制结束写事务中的子进程后重启恢复；过期 worker 的记忆与后续任务全部回滚；索引/入队/确认失败不留下半成品；两连接竞争不重复领取；v4 旧任务迁移与重复打开。尚需宿主轮询、远程嵌入计算移出写事务，以及大规模性能测试。v0.6 又增加了遗忘代次检查，旧计算结果丢弃并重新排队。

## 2. 部分完成（v0.6）：持久事实抑制；P1：完整内容删除

`forget_fact()` 已实现稳定断言 HMAC、来源片段抑制、核心字段来源清理、读取/索引复核，以及在途任务代次校验；`forget()` 调用同一入口。更换抽取器版本、重启和重建不能复活匹配断言或受抑制来源片段。旧副本与未来匹配断言的来源也会加入账本，防止后续换类型重提取。机制、回执和迁移见 [事实抑制](fact-suppression.md)。

这仍是 `logical_suppressed`：原始消息、SQLite 中的历史 payload 和备份保留。未知来源片段会保守隔离并在回执列出事件；跨独立事件的任意同义改写、无来源核心字段及活动检查点的任意自由文本尚不能可靠关联。尚无解除抑制的受控重新学习入口。

继续按三种操作推进：

- forget_fact：已实现本地持久抑制；待增加明确重新授权、持久操作回执及宿主旧上下文撤销。
- forget_event：清除事件正文，取消关联任务，更新来源列表、派生事实、摘要和核心投影。
- forget_scope：按用户/角色/领域清除，处理版本历史、缓存、索引与备份。

不能为删除一个事实而默认删除整条消息中其他无关事实。来源引用需要类型化为 event、memory、core_version，并区分“任一独立来源足以支持”和“多个前提共同成立”。当前 derived_from 采用前一种语义，不能表达 AND 推导。

参考：[Graphiti remove_episode](https://github.com/getzep/graphiti/blob/main/graphiti_core/graphiti.py#L2174)会更新来源列表，处理无剩余来源的事实，并重建受影响摘要。需要借鉴完整失效链，只有父来源检查还不够。

已验收：匹配断言与已知片段在重提取、重建、重启和核心恢复后不复活；同消息其他已定位片段保留。后续验收还包括正文擦除、恢复备份前同步删除账本，以及 forget_event 仅撤销一个独立来源时保留仍有其他来源支持的事实。

## 3. P1：宿主自动恢复与核心写入边界

目前只有 Python 门面和 CLI，没有接入真实聊天宿主。MCP 可支持主动查询，但不能单靠模型记得调用工具来保证首次回复前恢复身份。

建议先实现一个宿主适配器：新会话调用 bootstrap，每次请求模型前 retrieve 并按预算注入；实际消息落库后 observe；活动结束或压缩前保存检查点。角色 ID 持续存在，窗口 ID 只记录来源。

参考：[Celiums SessionStart hook](https://github.com/terrizoaguimor/celiums-memory/blob/4fcd62ced341536c3585deb8a31969e5c2145a82/packages/plugin-claude-code/src/hooks/session-start.mjs#L28)。宿主必须绑定可信 scope，不能把模型传入的用户 ID 直接当作授权。correct、checkpoint、expand_summary 等按 ID 操作的本地 API 目前仍依赖可信调用者。

v0.6 已迁移核心键：身份按用户/角色/领域、关系再按 relationship_id 独立版本化，并保存字段级来源。旧版未存核心领域，迁移只能按历史默认 real 归属，多世界旧库需要重新归属。`propose_core_update` 仍直接应用补丁，后续应分开候选与生效操作，验证证据、原因与字段权限。记忆围栏只帮助区分资料；工具和核心写权限仍由宿主控制。

验收：新进程第一条回复前恢复相同身份；恢复失败不覆盖原角色；历史正文无法直接修改核心或执行工具。

## 4. P1：中文语义检索与抽取质量

本轮修复了规则将引述、假设和复杂否定记成事实的若干错误，但示例规则仍不理解开放语言。默认哈希向量只有字面重合能力，两个同义改写观察项仍失败。

建议提供可选真实嵌入与模型抽取适配器，保留零依赖基线。零依赖是当前实现选择，用户并未要求禁止可选依赖或 API。先建立中文标注集，再选择多语言模型；记录模型版本、维度、查询/文档前缀、归一化策略并重建索引。验证超时降级与关键词回退，重新校准阈值。

抽取 JSON 仍需校验类型、说话者、模态、来源和有效期。候选可和相近旧记忆一起决策；没有充分依据时，不自动撤回旧事实。

参考：[Mem0 当前主线 additive 抽取](https://github.com/mem0ai/mem0/blob/main/mem0/memory/main.py#L918)、[cogmem-agent 混合检索](https://github.com/akira-001/pj-memory/blob/d0b2287e9a6fd2d19b0d77c88b48ea6169675250/src/cognitive_memory/search.py#L207)。实现随版本变化，不能将某一路径概括为所有模式都只增不改。

验收：同义改写、指代和多事实消息能召回；引用、愿望、虚构事件不会成为已发生的用户事实。分别衡量准确率和错误记忆率。

## 5. P2：完整双时间与可逆派生

目前有记录时间、事件时间、有效期和简化 as_of。本轮修复了时间边界和当前摘要泄露，但没有完整保存“何时得知旧事实失效”。

例如 20 日获知某偏好从 10 日起改变，查询“15 日时系统认为怎样”和“15 日的实际状态”应允许不同结果。修改同一行 valid_to 无法完整表达。

建议增加事实有效时间与系统记录时间的版本表或修订日志；核心和派生内容引用具体版本；去重保留合并日志，误合并可以重建。来源修订后先标 stale，再由可靠队列重算。

参考：[Graphiti 时间字段](https://github.com/getzep/graphiti/blob/main/graphiti_core/edges.py#L263)。有效期加 as_of 参数不能等同已经完成双时间数据库。养猫和对猫过敏可以同时成立，复杂事实的冲突也不能靠直觉直接判定。

## 6. P2：评估、人格成长和预算计量

183 个测试是工程回归；12 个固定检索用例中有 10 个计分、2 个观察项，不能证明长期人格稳定或中文语义检索质量。

建议建立独立中文陪伴评估集：跨会话恢复、称呼变化、共同经历、未完约定、旧事原话、反事实、删除后复活及多活动隔离。加入无依据负例，分别测证据召回、回答正确性、错误记忆、纠错生效、延迟和 token 成本。调规则用例与最终评估集分离。

[LongMemEval](https://github.com/xiaowu0162/LongMemEval)可补充通用能力评估，与中文集并行使用。对类别路由、48 小时分级、衰减和摘要做消融对照。核心成长先建立候选和用户反馈；访问次数不会增加真实性。

本轮预算覆盖完整检索上下文，但仍是启发式计量，bootstrap 尚无统一预算。下一步由宿主注入目标模型的 TokenCounter，分别约束身份、工作集、摘要和证据。
