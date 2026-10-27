# 受控重新学习（v0.23，schema 22）

本功能已实现。用户忘记某个事实后，可以通过一次新的明确重申，让可信宿主批准重新记住。批准会建立一条新记忆及持久回执；旧记忆、旧原话片段、已失效摘要和旧核心字段继续被屏蔽。它不恢复过去的历史或撤销全部遗忘。

## 宿主使用

```python
# 宿主先保存用户实际输入，独立核验“重申该事实并允许记住”的意图。
prepared = session.prepare_turn(
    "new-user-message", "我喜欢海边故事，现在允许你重新记住。", max_tokens=0,
)
receipt = session.approve_relearning(
    "mem_previously_forgotten",
    user_event_id=prepared.user_event_id,
    reviewer="trusted-host",
    reason="用户明确重申并授权重新记住",
    idempotency_key="relearn-approval-001",
)
print(receipt.memory_id)            # 新记忆 ID
print(receipt.old_records_restored) # False
```

`MemorySession.approve_relearning()` 只接受本会话当前用户事件，复核会话、完整作用域与活动授权。它是可信宿主 API，没有注册到 LLM 工具目录，也不接受模型自行宣称已取得用户同意。当前原生 Harness 没有审批界面；接入方可用 SDK 或本地 CLI 管理。

运行时管理接口为 `runtime.relearn_fact(memory_id, scope, confirmation_event_id=..., approved_by=..., reason=..., idempotency_key=..., source_span=...)`。`scope` 必须精确匹配用户、角色、关系、活动和领域；`None` 不表示通配。`source_span` 使用 Unicode 码点，省略时表示整条新确认原话。如果整条原话还覆盖其他被遗忘事实，应明确给出只属于本次重申的片段。

本地 CLI 示例：

```powershell
python -m memorydreaming --db .memorydreaming/memory.db relearn mem_old --user u1 --character c1 --relationship r1 --confirmation-event evt_new --approved-by local-operator --reason "用户明确授权重新记住" --key relearn-001
```

`evt_new` 必须已经通过 observe/prepare_turn 保存，且在最近一次遗忘后新写入。重复请求使用同一 key，返回同一回执且 `duplicate=true`。同 key 换参数、同一次确认换 key 重复授权都会被拒绝。

## 存储与算法

`fact_forgetting_barriers` 为每个抑制标记保存独立代次和最近遗忘屏障；`relearning_grants` 保存审批者、理由、目标、确认事件、新记录和 HMAC 绑定。正文不复制到授权表。显式遗忘才更新屏障，后台重新抽取被抑制候选不会撤销已经批准的重新学习。

1. 在只读快照中校验原事实有抑制标记，确认事件属于同一精确作用域、说话者为用户且未被遗忘。校验系统记录时间晚于最近遗忘屏障；不能用事件发生时间或旧消息代替新确认。
2. 创建新候选，保留所选事实正文、类型及模态，认识论状态为 `user_report`，只引用新的确认事件。新有效期从这次重申开始，不回填旧时间线。
3. 在写事务之外计算向量；候选计算本身不授予记忆读取或写入权限。
4. 在一个事务中重新检查幂等回执与内容代次，写入授权、新记忆、向量、实体投影、作用域代次和审计回执。依据变化或任何写入失败都整体回滚。
5. 读取时授权只豁免指定新记录的完整正文和新来源；保留其他事实的抑制检查。展开来源时仅对该新确认事件开放批准片段，旧原话仍然屏蔽。

再次遗忘同一断言会递增屏障代次，使旧授权失效并删除新记录。旧审批重试不能复活它，必须再取得新确认。屏障同时覆盖当时已有事件的最大记录时间，时钟回拨也不能把旧确认变成新确认。忘记确认事件同样撤销其支持。

授权是精确记录授权，没有全局解除同义事实的抑制；普通未来重述仍不会绕过遗忘账本。换抽取器重抽新确认也不会创建额外授权。旧核心字段的重新设置需要另外的核心审批规则，本功能不会自动恢复人格或关系字段。

## 迁移与验收

schema 21 及更早版本原子升级为 22，保留旧抑制标记，建立保守屏障并要求升级后的新确认。完整及作用域快照携带授权与屏障；导入旧快照时为缺少屏障的标记建立新屏障。scope 物理清除同时清除相应授权和屏障。

`tests/test_relearning.py` 覆盖新记录重启运行时恢复、原话回溯、局部开放、幂等、旧/非用户/跨作用域依据拒绝、再次遗忘、时钟回拨、回执失效、重提取防复活、事务失败、事务外编码、并发遗忘、快照与迁移、CLI。它验证本地引擎与宿主接口，不代表实际 Harness 已有审批界面。

2026-10-27 验证：13 项重新学习测试及完整 538 项回归通过。已构建 `memorydreaming-0.23.0-py3-none-any.whl` 并逐项对照包内源码；临时目录独立安装后，通过 CLI 授权、另一个进程幂等重试和新进程检索，核验新来源、单份回执与审计链。未调用模型 API，也未改动正在运行的 Harness。
