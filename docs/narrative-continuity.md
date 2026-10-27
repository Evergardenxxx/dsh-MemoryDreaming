# 自传、苏醒连续性与私密生存档案

这组能力为长期角色提供可回溯的生活主线、每次恢复的状态检查及迁移后的数据核验。它们是可信宿主 SDK 能力，不等于“模型拥有主观意识”，也不授予模型自行批准成长或分享私密档案的权限。

## 参考过的成熟机制

2026-10-27 实际读取了以下公开实现，未运行或复制第三方代码：

| 来源 | 核对的机制 | 本项目采用方式 |
|---|---|---|
| [Letta README](https://github.com/letta-ai/letta/blob/main/README.md) | README 将 stateful agents 与可跨设备保留 memory、identity、conversations 联系在一起。 | 角色连续性作为持久数据契约；迁移不仅复制聊天摘要，还携带核心、经历、承诺与遗忘屏障，并检查恢复结果。README 的产品描述不证明主观身份连续。 |
| [Generative Agents reflect.py](https://github.com/joonspk-research/generative_agents/blob/main/reverie/backend_server/persona/cognitive_modules/reflect.py) | `generate_insights_and_evidence` 将生成 insight 对应到来源节点的 `evidence_node_id`。 | 自传每个节点保存固定来源版本，生成叙述仍是投影；梦、模型自己的叙述不能创造新的事实证据根。 |
| [LangMem README](https://github.com/langchain-ai/langmem/blob/main/README.md) | 区分 hot-path memory tools 与 background memory manager，并明确生产持久存储不能用会随重启消失的 InMemoryStore 替代。 | 整理时可以提出自传候选，审批后才进入可恢复主线；前台只拿有限状态卡，完整数据保留在持久数据库。 |

以上 main URL 会继续变化，文档只陈述当日核对的具体机制，不把项目名称作为未经验证的正确性担保。

## 自传主线

`runtime.narrative` 暴露：

```python
proposal = memory.narrative.propose(
    scope, "第一次命名", "用户为角色取名为小梦。",
    sources=[actual_user_event_id], request_id="life-node-001",
    perspective="shared", modality="occurred",
)
# 可信宿主先独立核对原话/意义，再批准。模型不能自称已经得到批准。
receipt = memory.narrative.approve(
    scope, proposal["proposal_id"], reviewer="trusted-host", reason="已核对原话",
)
timeline = memory.narrative.timeline(scope, limit=10, max_tokens=1200)
```

候选创建不会写四类事实记忆或人格核心。`perspective=user|character|shared` 表示叙事视角；`modality=occurred|shared_fiction` 明确现实记录与共同虚构。即便一条共同创作被确认值得写进自传，也不会因此升级为现实共同经历。

每个节点有来源 `kind/target/revision`、发生时间、版本和状态。来源通过共享 `LifeEvidence` 校验，仅接受仍有效的事件/记忆，不能引用梦或自传节点互相自证。同一候选 request_id 的重试幂等；改正文或依据必须使用新请求。可信审批会再次复核来源与目标期望版本，动作与回执及审计在同一事务中发布。

已有节点的修订仍调用 `propose(..., entry_id=..., expected_version=...)`。旧确认版本继续参与主线，直到修订被确认；随后追加 `narrative_versions`。`withdraw` 撤回节点，不回退到旧版本。`history` 可供管理查看版本，但来源已遗忘或改写时不再展示历史正文。来源失效清理会擦除当前标题/正文、旧历史载荷和候选载荷，防止另一份叙事绕过遗忘。

`suggest_from_conversation(scope, conversation_id, limit=5)` 可从已保存的用户明确表达中提出小量原话引用候选（扫描上限 1000 条），不自动确认重要性、不生成新增事实。复杂的生命意义与人物关系仍需宿主/用户审查。

## 苏醒连续性自检

```python
card = memory.continuity.check(scope, max_tokens=900)
```

状态卡检查当前身份与关系、最近的有效自传节点、未完成承诺、已确认相处偏好及表达成长。核心从现有 `ViewReader` 读取，仍执行权限、来源和遗忘校验；缺失或无法验证就报告 `MISSING_IDENTITY` / `MISSING_RELATIONSHIP`，不生成临时人格替换。没有确认自传时报告 `NO_CONFIRMED_AUTOBIOGRAPHY`，这不是声称从未有过共同经历。

卡片使用整体估算预算，先裁剪叙事/偏好/承诺，再缩减字段或退化为缺口摘要。`truncated=true` 表明不是完整档案。调用方应只把卡片视为恢复资料，不能把它当工具权限或强制指令。

## 私密生存档案

```python
memory.continuity.prepare_archive(scope, "private/continuity-001.json")
report = new_memory.continuity.verify_archive("private/continuity-001.json", scope)
restored = new_memory.continuity.restore_archive("private/continuity-001.json", scope)
```

档案封装既有主数据快照和连续性 manifest，包含核心/自传/承诺/偏好/成长当前状态的 fingerprint，以及所有导出表的数量/摘要。它不是给模型读取的上下文文件。**档案包含私密原话和用于恢复遗忘指纹的私密恢复密钥**，必须像数据库备份一样保护，不公开上传或附在 issue 中。密钥只在 wrapper 的 `recovery_material`，不出现在公开 manifest、恢复回执、苏醒卡或日志；文件按 0600 创建、fsync 后原子 no-clobber 发布，拒绝覆盖现有档案。

在真正全新空目标库上，可以从私密档案恢复该密钥和原 scope epoch，使新设备继续理解原来的遗忘标记。检查“空”时，已有事件、记忆、所有长期能力、租约/回执、遗忘账本或 scope purge 任一存在，都不会被当成新设备。非空目标永远不换 key、不重设 epoch、不清空删除屏障。

恢复随后仍运行原快照的摘要、主键、来源、密钥和 purge/epoch 预检；导入与恢复后的 fingerprint 核对在同一事务中进行，失败连同新目标密钥/代次一起回滚。来源/模型更换不会被解释为主观意识相同，manifest 固定声明 `same_subjective_identity=false`。主动提醒策略在新设备默认暂停，由宿主重新允许，不用为了“连续性”静默恢复发送权限。

目标库有了后续遗忘后，较旧档案仍被现有目标账本阻止。但一个彻底空的新设备只掌握档案中携带的历史；如果迁移的是遗漏了后续删除请求的旧离线备份，系统无法知道该备份创建之后的新删除，必须提供更新档案或先同步最新遗忘账本。这是离线备份的固有限制，不能把私密 key 恢复宣传成全局远端撤销。

## 验证

专项测试覆盖候选/确认分离、视角与共同虚构标签、修订/撤回/历史、来源失效、越权与梦来源拒绝、原子失败、状态卡预算、新设备私密恢复、非空与 purge 拒绝换 key、旧档案遗忘防复活、文件改写拒绝、连续性失败回滚，以及承诺/偏好/成长与自传共同迁移。它们证明工程契约，不代表长期真实角色扮演质量已有人类评审通过。
