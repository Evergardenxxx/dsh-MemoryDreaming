# 文档导航

首次使用从[快速上手](quickstart.md)开始，贡献代码先看[开发导读](development-guide.md)。当前能力状态以[项目首页](../README.md)、[路线图](../ROADMAP.md)及[完整待办](open-problems.md)为准；早期架构提案描述的目标不自动表示已实现。

## 使用与发布

| 文档 | 内容 |
|---|---|
| [快速上手](quickstart.md) | 环境、安装、首次设定、指令和排错 |
| [原生插件说明](../integrations/deepseek-harness/README.md) | Harness配置与真实用户管理指令 |
| [桌面记忆管理](desktop-management.md) | 原生侧栏、记忆/候选、首次角色设置、窗口按钮及睡眠进度 |
| [窗口、睡眠与苏醒](window-sleep-wake.md) | 按窗启用、分批审阅、梦与可逆巩固 |
| [长期角色](long-term-role.md) | 自传、承诺、偏好、成长、自检与私密档案 |
| [安装发布](releasing.md) / [GitHub发布准备](github-publication.md) | 隔离安装、构建、附件、远程发布步骤 |
| [贡献指南](../CONTRIBUTING.md) / [安全报告](../SECURITY.md) | 提交要求、合成复现与私密数据边界 |

## 架构与接口

| 文档 | 内容 |
|---|---|
| [架构总览](architecture-overview.md) | SVG、Mermaid、普通回合和睡眠流程 |
| [系统架构](system-architecture.md) / [开发导读](development-guide.md) | 当前模块职责、事务与扩展点 |
| [架构v0.2](architecture-v0.2.md) / [算法v0.2](algorithms-v0.2.md) | 核心/四类记忆设计与形成、路由、排序规格 |
| [LLM接口v1](llm-interface-v1.md) / [宿主适配](host-adapter.md) | 绑定、生命周期、引用、协议与SDK |
| [接口Schema和样例](contracts) | JSON契约、候选与响应样例 |
| [参考来源](references.md) / [早期v0.1](memory-engine-architecture-v0.1.md) | 已核对机制、具体引用与历史设计 |

## 来源、变更与角色投影

| 文档 | 内容 |
|---|---|
| [AND/OR证据组](evidence-groups.md) / [事实抑制](fact-suppression.md) | 来源存活、循环拒绝与遗忘防复活 |
| [核心提议](core-proposals.md) / [变更审批](correction-approval.md) | 模型提议、可信审批和原子回执 |
| [核心/摘要历史](core-summary-history.md) / [重新学习](relearning.md) | 双时间、修订、历史恢复与明确新授权 |
| [自传与档案](narrative-continuity.md) | 来源主线、连续性状态卡和私密新设备恢复 |
| [陪伴偏好](companionship-preferences.md) / [承诺与主动性](intentions-proactivity.md) | 场景、修订、用户确认与投递队列 |
| [睡眠参考](sleep-design-references.md) | 梦、回放与遗忘的研究/工程边界 |
| [便携快照](portability.md) | 预检、原子导入和删除屏障 |

## 检索、后台与生命周期

| 文档 | 内容 |
|---|---|
| [候选缓存](retrieval-cache.md) / [摘要生命周期](summary-lifecycle.md) | 缓存代次、失效、撤回和回源 |
| [实体索引](entity-index.md) / [语义嵌入](semantic-embedding.md) | 图/实体投影、本地模型与原子重建 |
| [可靠任务](reliable-consolidation.md) / [事务外计算](optimistic-computation.md) | 租约、崩溃恢复、慢计算与提交复核 |
| [DeepSeek抽取](deepseek-extraction.md) / [请求调度](deepseek-scheduling.md) | API候选契约、失败退避与配额 |
| [请求预算](request-budget.md) / [计量接入](tokenizer-integration.md) | 完整请求、精度声明与当前模型边界 |
| [原生生命周期](native-lifecycle.md) / [运维恢复](operations-and-recovery.md) | 超时、取消、投递补偿、指标与告警 |

## 证据与待办

[安装版运行时验收](installed-harness-acceptance.md)、[真实权重与质量](quality-acceptance.md)、[性能基线](performance-2026-10-27.md)、[规则抽取基线](evaluation/extraction-zh-v1-rules.json)、[审查记录](review-2026-10-26.md)、[完整待办](open-problems.md)。工程回归、合成质量、真实模型与独立人工验收分开记录。
