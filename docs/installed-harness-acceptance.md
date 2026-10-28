# 安装版 Harness 运行时验收（引擎 v0.30 / 原生 v0.17 / schema 27）

使用本机已安装 Harness 的真实 Cordis、agent loop、Session、工具、压缩、token meter 与 JSONL 持久化模块，在临时目录验收插件。没有复制用户 profile、读取登录凭据或替换正在运行的应用。模型供应商是确定性本地测试适配器。

## 已发现并修复的兼容问题

安装版 Cordis 4.0.5 通过 `ctx.effect` 返回的清理函数管理插件卸载，不会自动发出插件 `dispose` 事件。旧插件只监听事件，实际卸载时可能留下 Python bridge。原生 v0.14 起优先注册 effect，旧接口保留事件回退；两条路径返回同一个有界、幂等关闭 Promise。此问题来自运行实际安装模块，此前小型 fixture 的事件回归不能覆盖。

## 可重复执行

```powershell
python scripts/accept_installed_harness.py --asar "C:\Users\YOUR_USER\AppData\Local\Programs\DeepSeek Harness\resources\app.asar" --output dist/installed-harness-acceptance.json
```

v0.30新增`--desktop`，在原15条路径之外执行12条原生管理通道验证：

```powershell
python scripts/accept_installed_harness.py --asar "C:\Users\YOUR_USER\AppData\Local\Programs\DeepSeek Harness\resources\app.asar" --desktop --output dist/desktop-preview/installed-runtime.json
```

脚本只读取指定 ASAR 的公开程序模块及相邻 unpacked 原生依赖，提取到临时目录；不读取 userData、账号、Cookie、真实会话或凭据。第三方代码副本不加入源码/发布包。Node.js 需满足该安装版要求；本机使用 Node 24.18.0。

## 15 条隔离路径

`checked` 数组记录以下实际运行时检查，每项均执行断言：

1. 实际 agent 首个请求前注入单个原生记忆节点。
2. 多轮消息替换自有节点，不无限追加。
3. 实际工具调用、tool result 与下一次模型步骤。
4. Cordis effect 卸载/重载，释放进程退出监听。
5. 实际取消路径提交的文本保存为 interrupted。
6. 供应商异常得到框架处理。
7. 异常后的下一轮恢复正常。
8. 实际 BasicCompactionEngine 提交压缩，下一轮重建记忆。
9. flush 后 Node 进程直接退出，跳过插件/框架清理，退出兜底终止 bridge。
10. 新 Node 进程取得实际 JSONL 的 OS 写租约并 resume。
11. 普通窗口默认关闭，开启只影响指定窗口。
12. 宿主模型通过独立请求审阅完整睡眠分片。
13. 结构化自传、承诺、陪伴偏好与表达成长指令按来源和版本生效。
14. `记忆自检` 返回连续性状态卡，普通对话只恢复有界角色资料。
15. 新窗口苏醒后读取带虚构标签的梦，关闭后停止注入。

数据库另行核对已知验收输入、投递总数和一条 interrupted 回复；这些计数用于校验持久化，不证明模型理解或长期角色质量。主动提醒只配置默认关闭的本地队列，本脚本不发送通知，也不验证任何外部渠道。

2026-10-27 本机 15 条路径结果为 passed。Harness 运行时包版本 `0.2.1-rc.3`，Cordis `4.0.5`；ASAR SHA-256 为 `04b32e3a872826ba15cfb2081c6a929a8d4ecaa65e09a2543e5a6b30e2eff1f0`。报告保留 `real_desktop_ui=false`、`live_model=false` 和每个检查名，不以测试适配器替代当前账号模型。

## v0.30新增12条管理通道路径

2026-10-28在同一安装版公开模块上执行，合计27条通过。新增检查运行实际Cordis Gateway、Typert Registry和ClientModuleRegistry：

1. 发现并调用管理控制器的公开SRC描述符。
2. 即使伪造页面参数并放宽全局策略，非拥有者仍不能调用。
3. 拒绝RPC额外命名参数。
4. 未知页面操作不能成为任意桥接操作。
5. 读取固定角色绑定不启用任何窗口。
6. 跨服务调用保持真实拥有者身份，窗口指令接纳保持幂等。
7. 停止运行中的睡眠仍保持拥有者身份。
8. 传输取消可到达待处理的管理读取。
9. 卸载中止活动读取并释放控制器状态。
10. 注销后的服务不能被Gateway访问。
11. 客户端注册表识别插件清单和桌面依赖。
12. 客户端注册表服务实际lazy bundle，加载阶段不提前调用页面RPC。

这12条使用确定性的窗口指令接纳和bridge替身；真实Python桥接、SQLite、睡眠编排与页面交互由独立的12条浏览器路径验证，详见[桌面管理](desktop-management.md)。两组结果不能合并宣称已经运行用户Electron窗口或登录模型。

## 尚需桌面与线上验证

用户此前实际测试已经留下跨窗口偏好回忆、来源链、中断回复和无依据拒答记录，但它们不覆盖 v0.29角色指令和v0.30管理页面的完整组合。新版本仍需升级后在临时窗口实测“记忆开启—管理角色资料—睡眠—新窗苏醒”，以及当前模型的回应质量与请求计量。

本机已登录 Harness 在产品上可用于真实测试；此前 computer-use 连接因 native pipe 不可用失败，本地 API 只读探查返回 401，因此未沿用用户登录界面发送自动验收请求，也未提取 Cookie 或绕过认证。以后恢复桌面执行入口，可以使用 [逐题评测包](quality-acceptance.md) 在专用会话收集真实返回。

当前 V4.1 tokenizer/线上 usage、图像和文件投影、独立人工质量复核以及实际通知渠道均保持待验收。隔离结果只覆盖上述27条工程路径；角色资料迁移和苏醒状态卡也不表示模型拥有相同的主观身份。使用指令见 [插件说明](../integrations/deepseek-harness/README.md)。
