# 类型化 AND/OR 证据组（v0.24，schema 23）

此功能已实现：同一组的前提必须全部成立（AND），不同组任一完整成立即可支持目标（OR）。依据可以是原始事件 `event`、记忆 `memory` 或当前明确授权的核心版本 `core_version`。记忆类型与认识论状态保持原值；支持组描述来源依赖，不验证推理本身是否正确。

## 可信宿主接口

```python
from memorydreaming import Scope

scope = Scope("u1", "c1", "r1")

# 每条引用由引擎在当前快照签发并锁定版本，不能直接用一个字符串 ID 代替。
free_time = memory.evidence_ref("memory", "mem_user_available", scope)
open_place = memory.evidence_ref("event", "evt_place_open", scope)
explicit_plan = memory.evidence_ref("core_version", "relationship", scope)

receipt = memory.set_evidence_groups(
    "mem_trip_suggestion", scope,
    [[free_time, open_place], [explicit_plan]],
    expected_version=0,  # 第一次绑定；更新时使用回执的 version
    reason="有空且地点开放，或当前已批准的关系安排直接支持该建议",
)
status = memory.inspect_evidence("mem_trip_suggestion", scope)
print(status["supported"], status["version"], status["groups"])
```

这些是可信本地 SDK 接口，不注册为模型写工具。宿主负责确认推导与用户授权。目标必须是同一完整 scope 的 active 记录；父依据只允许同用户、角色、关系、领域的共享范围或同一活动，不接受别的活动为共享结论提供依据。需要多个来源的派生记录通过支持组建模，无须复制所有父事件到目标的 `source_event_ids`。

每目标 1..32 个组，每组 1..20 项。同组重复来源去重，空组、无效类型、过期引用、跨范围来源、错误 `expected_version` 和没有外部根的支持链会被拒绝。更新是原子的；任何失败不保留半份支持集、成员或审计回执。

`inspect_evidence` 返回版本、完整性、当前是否有支持与已绑定的类型化成员。`refresh_evidence()` 是可信管理入口，撤回已经失去支持的派生记录；不会自动恢复 retracted/deleted 记录。

## 版本与求解

`evidence_sets` 保存目标内容指纹、支持集版本、精确 scope 和审批理由；`evidence_groups` 表示 OR 分支；`evidence_members` 依次保存组内 AND 前提及其内容版本。事件引用锁定原话和时间；记忆引用锁定内容、来源、效力及支持集版本，读取次数和热度不参与版本；核心引用指向指定 scope/kind/version，换到新核心版本后旧依据失效。

求解从仍有效的事件、可信本地根记录及无来源的明确核心设定开始。为每组计算未满足前提数，从根逐步激活满足全部前提的组和目标。每节点只激活一次，纯循环不会进入激活队列。单次求解为 O(V+E)；当前管理刷新按精确 scope 扫描已配置的派生记录，再求必要的祖先闭包，没有实现增量后台求解。

遗忘事实、事件或 scope，结构化纠错、核心替换和支持集重绑定后会在同一事务刷新派生状态，清理失效记录的向量、实体投影并使摘要失效。直接改原文或底层仓储时，动态读取校验会先阻止失效支持被使用；可信调用方可再调用 `refresh_evidence()` 持久化撤回。内容代次触发器覆盖三张新表，缓存不能保留旧支持结果。

默认单次最多 4096 节点、16384 条边。触及规模边界时 `complete=false`，读取保守拒绝；管理刷新不会把一次不完整计算当成“全部记录都该撤回”。大规模增量求解和历史支持组回放不属于当前能力。

## LLM 读取

原生插件、SDK 检索、摘要、向量与核心字段恢复都会复核支持状态。`memory_search` / `memory_read` 对显式支持集只展开一组完整、存活的 AND 依据；失效 OR 分支不会妨碍其他独立来源。引用继续使用会话签发的 `mref_...`，不向模型提供数据库写权限。

记忆记录增加可选字段：

```json
{
  "support": {
    "mode": "all",
    "evidence_version": 1,
    "source_refs": ["mref_first_premise", "mref_second_premise"]
  }
}
```

`all` 指本次展开的整组前提须一起理解，不表示只有一个 OR 分支。每组最多 20 项，避免来源列表截断后丢失必需前提。预算不足时沿用整体记录裁剪，不把半个 AND 组展示成完整依据。核心版本来源也可以通过 `memory_read` 展开。没有增加新的模型工具目录。

## 兼容与迁移

schema 22 及更早数据库原子升级为 23；旧 `derived_from` 链接继续使用 OR 语义，不猜测旧记录本来是不是 AND。显式设置支持组后，目标使用新组替代旧派生链接的支持语义；原有事实抑制及原消息保护仍然生效。兼容的无来源本地直写记录不等同显式空组，空组始终无效。

完整/作用域快照携带支持集、组和成员，导入预检拒绝缺少目标或前提的快照。作用域快照仍须满足既有遗忘代次同步契约；范围外共享前提未包含时不能伪装成自包含快照。scope 物理清除会级联清理对应支持集，缺失来源不会成为新的证据根。

新增 18 项验收覆盖 AND/OR、事件与核心来源、纠错、版本冲突、作用域、循环、规模上限、原子回滚、LLM 引用、快照和 v22 升级。真实模型不会自动创建多事件推导组；当前由可信宿主 SDK 显式确认，后续可独立接入审批界面。

2026-10-27 验证：完整 556 项回归通过；已构建 `memorydreaming-0.24.0-py3-none-any.whl` 并逐项比较包内源码。在临时目录独立安装后，由新进程恢复支持组、撤销一个 OR 分支、通过现有 `memory_search` / `memory_read` 展开剩余完整来源，再撤销最后一组并核验派生撤回与审计链。未调用模型 API，也未改动正在运行的 Harness。
