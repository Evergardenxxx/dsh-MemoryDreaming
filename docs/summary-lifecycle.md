# 派生摘要的撤回与重建

摘要属于可重建的索引投影，来源记忆保存在主表。当前支持 `active/stale/withdrawn/rolled_back` 状态，只有来源仍可验证的 active 摘要参与检索。

- 来源变更将 active 摘要标记 stale，普通整理任务可以重建 stale 摘要。
- `runtime.withdraw_summaries(ids, scope)` 隐藏人工撤回的卡片，保留来源与旧投影。来源更新和后台重建不能解除撤回。
- `runtime.rollback_summaries(ids, scope)` 将卡片标记 rolled_back，具有同样的阻止自动重建规则。这个 API **不恢复某个历史版本的摘要正文**；摘要版本日志和历史内容回滚仍待实现。
- 可信宿主显式调用 `runtime.build_summaries(scope, reactivate=True)`，才能让该范围内参与本次主题构建的撤回卡片重新激活。普通 `build_summaries(scope)` 和后台任务默认保持撤回状态。

重建复用摘要 ID；来源和正文变化后，旧会话引用仍按 revision 校验失效。人工批量状态变更先在同一事务中校验全部 ID 和作用域，再更新状态和 journal；任意一项失败不会应用其他项。重复设置相同状态不会增加 journal 或推进内容代次。构建的来源读取、状态检查、全部主题写入和失效处理也在一个事务内提交。

schema 17 到当前版本的迁移会原子重建旧 CHECK 约束，保留 ID、来源和正文。专门回归覆盖旧约束拒绝 withdrawn、迁移后可写、重复打开，以及迁移失败后旧表仍可使用并可重试。
