# 架构总览

本文对应引擎0.30.0 / 原生插件0.17.0 / schema27的当前实现。早期设计目标保留在[架构v0.2](architecture-v0.2.md)中，尚待验收的能力见[路线图](../ROADMAP.md)。

![MemoryDreaming系统架构](assets/architecture.svg)

SVG可直接用于GitHub首页、演示或导出；[Mermaid源图](diagrams/architecture.mmd)用于修改结构。以下Mermaid图在GitHub中可直接渲染，SVG是同一架构的简化展示。

## 系统边界

```mermaid
flowchart TB
  U["真实用户 / 独立聊天窗口"] --> N["原生 Cordis 插件：按窗开关、生命周期、完整请求预算"]
  U --> UI["Harness 原生记忆管理页面"]
  UI --> O["拥有者 RPC：来源查看、明确审批、窗口指令"]
  O --> B
  O --> N
  N --> B["本地 JSONL Bridge / HostAdapter：固定可信绑定"]
  B --> R["MemoryRuntime：统一资源与事务"]
  R --> C["受控核心 + 工作 / 情景 / 语义 / 程序记忆"]
  R --> Q["检索：时间、关键词、向量、实体、融合与来源复核"]
  R --> P["角色投影：自传、承诺、偏好、受控成长"]
  R --> S["睡眠审阅状态 / 独立虚构梦 / 苏醒自检"]
  R --> W["后台任务：抽取、去重、摘要、租约与退避"]
  R --> F["纠错、遗忘屏障、私密档案与恢复核验"]
  C --> D[("SQLite：主数据、历史、任务、回执与审计")]
  P --> D
  S --> D
  W --> D
  F --> D
  Q --> D
  Q --> V["当前有效、预算内的恢复资料"]
  V --> N
  N --> L["Harness 当前模型：前台回复 / 独立睡眠审阅"]
  L --> N
  W -. "可选 DeepSeek API 抽取" .-> A["配置的 DeepSeek API"]
  Q -. "可选本地语义编码" .-> E["本地权重 / 编码进程"]
```

原生插件不注册MCP工具目录。SDK保留查询/审批接口，仓库另有早期只读兼容模块；它们不参与当前Harness原生链路。user/character/relationship/activity/domain决定长期资料边界，conversation_id决定窗口来源与开关；换窗口不会创建一套新的角色记忆。

管理页面使用宿主共享客户端与原生RPC，不新增监听端口。`DesktopController`核对真实拥有者身份，`DesktopService`复用同一引擎的来源和版本检查；窗口按钮通过宿主的真实用户指令通道执行。页面数据不进入模型上下文，也不会因打开页面而开启任何对话窗口。使用和剩余界面工作见[桌面管理](desktop-management.md)。

## 普通回合

```mermaid
sequenceDiagram
  actor U as 用户
  participant N as 原生插件
  participant H as Bridge / HostAdapter
  participant D as 引擎 / SQLite
  participant L as 当前模型
  U->>N: 当前窗口的新消息
  alt 当前窗口关闭或睡眠
    N->>L: 普通宿主流程，插件不记录/检索新消息
  else 当前窗口已开启
    N->>H: prepare_turn(固定绑定、message_id、预算)
    H->>D: 幂等保存事件，恢复核心与查询候选
    D-->>H: 当前有效资料、来源与缺口
    H-->>N: 有界恢复资料
    N->>N: 替换自有记忆节点，再核验完整请求
    N->>L: 预算允许的请求
    L-->>U: 宿主实际显示回复
    N->>N: 写入投递补偿 outbox
    N->>H: after_delivery(实际显示文本、稳定ID)
    H->>D: 原子保存投递与回执
  end
```

后台抽取/编码不占用写事务；最终发布重新检查来源、内容代次、遗忘屏障和租约。模型只返回候选，核心/角色变化仍要求可信宿主审批；完整JSON用户命令是独立核对原话后的显式快捷路径。

## 睡眠与新窗口苏醒

```mermaid
flowchart LR
  A["已开启窗口：记忆睡眠"] --> B["冻结本窗已保存来源与版本"]
  B --> C["领取一个有界分片"]
  C --> D["宿主当前模型独立审阅，无前台长历史/工具"]
  D --> E["校验覆盖、来源、遗忘代次与租约，保存候选笔记"]
  E --> F{"全部分片已完成？"}
  F -- 否 --> C
  F -- 是 --> G["原子发布虚构梦与可逆召回权重，窗口睡眠"]
  G --> H["新窗口：记忆苏醒"]
  H --> I["恢复有效核心/角色资料、缺口、最近有效梦"]
```

梦是独立`generated_fiction`派生物，不能作为事实或人格证据。睡眠笔记也不自动变成已批准事实。来源撤销会传播到梦、角色投影、历史展示与恢复卡。主动提醒队列和睡眠没有自动外发权限。

## 进一步阅读

模块和扩展点见[系统架构](system-architecture.md)，形成/路由/时间/排序见[算法规格](algorithms-v0.2.md)，对外绑定/引用/预算见[宿主指南](host-adapter.md)。[睡眠说明](window-sleep-wake.md)、[角色连续性](long-term-role.md)和[来源组](evidence-groups.md)解释各自的行为及验收边界。
