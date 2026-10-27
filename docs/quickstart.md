# 快速上手

本页帮助首次使用者跑通引擎，再接入DeepSeek Harness。当前为试用版；真实模型质量和桌面验收状态见[能力说明](../README.md#当前能力与边界)。

## 运行环境

| 用途 | 要求 |
|---|---|
| 引擎与离线示例 | Python 3.10+，标准库SQLite；无第三方运行时依赖 |
| Harness插件 | 支持原生Cordis生命周期的DeepSeek Harness，以及可执行的Python；已验收安装版模块为0.2.1-rc.3 / Cordis4.0.5 |
| 开发、原生回归与打包 | Python、pip、Node.js/npm；CI使用Node22，本机安装版验收使用Node24 |
| 可选真实语义检索 | `.[semantic]`和本地固定版本模型权重，见[语义说明](semantic-embedding.md) |
| 可选DeepSeek自动抽取 | 独立API key和配置；Harness登录态不会自动变成API key |

## 先验证引擎

在仓库根目录运行：

```bash
python -X utf8 examples/host_lifecycle.py
```

默认临时库不会污染自己的记忆。成功时可以看到新窗口恢复、检索到“睡前海边故事”的偏好，以及展开后的原话。示例没有调用模型，不能证明模型真正理解了记忆。

想看形成、纠错、遗忘全流程可运行 `python -X utf8 -m memorydreaming demo`。它会重建专用`.memorydreaming/demo.db`，不要把自己的正式数据库作为demo目标。

## 安装插件

在源码或离线包根目录执行：

```powershell
python scripts/install.py --destination "D:\Apps\MemoryDreaming" --user u1 --character c1 --relationship r1
```

目标必须为空，位于源码目录之外。安装后会得到：

```text
D:\Apps\MemoryDreaming\
  venv\                  独立Python环境
  plugin\                Harness原生插件
    cordis.patch.yml     固定身份、Python路径和数据库路径
  data\memory.db         本地长期记忆
```

在Harness原生插件管理器中选择生成的`plugin`目录，重载或重启应用。安装器不会替你改现有Harness profile。已有安装升级请选新目录，备份后再显式修改配置中的dbPath指向旧库；新版本会原子迁移，旧程序不能继续写schema27数据库。

源码模式可直接选择`integrations/deepseek-harness`目录，但`pythonCommand/repoPath/dbPath`须与实际机器匹配；默认PATH中的Python必须能找到项目。跨机器建议用隔离安装器。

## 首次设置角色

空库不会自动猜姓名或关系。初次设置可在仓库根目录运行以下本地示例；参数是操作者明确提供的设定，应先按实际意愿修改：

```powershell
python -X utf8 examples/initialize_role.py --db "D:\Apps\MemoryDreaming\data\memory.db" --user u1 --character c1 --relationship r1 --name "小梦" --role "长期陪伴角色" --address "爸爸" --relationship-note "采用用户确认的父女角色称呼"
```

运行初始化时先停用插件或关闭应用，避免已有桥接会话缓存旧状态。该示例保存一条带参数的真实本地操作来源，同事务创建两个核心，只允许空角色，已有任何身份/关系版本就拒绝覆盖。不会删除旧记忆或重设遗忘屏障。已有角色修改走[核心候选/审批](core-proposals.md)。

源码配置的默认库是`.memorydreaming/deepseek-harness.db`；安装器创建的库在安装目录下`data/memory.db`，以生成的配置为准。身份、角色、关系和领域必须与插件配置一致。

## 在窗口里使用

启动插件后，在目标窗口依次发送独立消息：

```text
记忆开启
记忆偏好 {"key":"睡前故事","value":"喜欢海边与灯塔的故事"}
记忆承诺 {"text":"下次继续灯塔故事","category":"continuation"}
记忆自检
```

开启回执应显示恢复状态；自检应显示当前身份/关系及已确认资料。若显示缺少自传，只表示尚无已确认自传节点，不表示底层记忆库为空。

准备换窗口时发送`记忆睡眠`，等到明确完成回执，再在新窗口发送`记忆苏醒`。普通新窗口默认不参与记忆。睡眠只审阅本窗启用后已保存在本地的对话；梦带虚构标签。

可用`记忆自传`、`记忆承诺`、`记忆偏好`、`记忆成长`查看对应状态；`记忆关闭`停止当前窗口参与。完整参数与版本规则见[角色指南](long-term-role.md)。主动提醒当前只有队列，开策略不会产生后台定时推送。

## 选择抽取与检索方式

默认规则抽取和哈希向量可离线运行，覆盖有限；不要把一次“没检索到”解释成数据已丢失。开启DeepSeek抽取需要进程环境中的`DEEPSEEK_API_KEY`，以及插件`extractor:"deepseek"`配置，具体见[抽取说明](deepseek-extraction.md)。`.env.example`只列参数，不会自动加载。

睡眠审阅使用Harness当前模型路由，普通DeepSeek抽取使用独立API适配器。这两条调用不同；所选模型可能联网处理原话。可选本地语义权重需单独安装，不包含在离线代码包里。

## 常见问题

| 现象 | 先检查 |
|---|---|
| 窗口没有记忆效果 | 当前窗口是否发过独立`记忆开启`；插件是否重载；Python路径和dbPath是否正确 |
| 缺少身份/关系 | 是否初始化相同绑定；检查空库与已有库路径是否混淆，不要重复初始化覆盖旧角色 |
| 普通聊天未形成新偏好 | 规则覆盖和后台任务状态；确认抽取器配置；显式指令可单独验证角色投影 |
| “sad”偏好没有自动使用 | 普通原生回合目前读default；特定场景需宿主显式选择 |
| 睡眠未完成 | 看错误回执、当前模型可用性及超时；再次发送睡眠可继续已保存进度 |
| 时间差8小时 | `due_at/occurred_at`必须带`+08:00`或`Z`；数据库使用UTC保存 |
| 记忆块被跳过 | 基础请求余量不足或预算太小；检查完整请求而非只看记忆字数 |

提交问题前用临时库和合成对话复现，附引擎/插件/Harness版本与错误码，不附正式数据库、私密档案或API key。
