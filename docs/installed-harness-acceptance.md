# 安装版 Harness 运行时验收（v0.27 / 原生插件 v0.14）

本轮直接使用本机已安装 Harness 的实际 Cordis、agent loop、Session、工具、压缩、token meter 与 JSONL 持久化模块进行隔离验收。没有复制用户 profile、读取登录凭据或替换正在运行的应用。

## 发现并修复的问题

当前安装版使用 Cordis 4.0.5，其插件卸载通过 `ctx.effect` 返回的清理函数管理，并不发出插件 `dispose` 事件。旧插件只监听 `ctx.on('dispose')`，在实际卸载时可能留下 Python bridge。v0.14 改为优先注册 effect 清理；旧宿主与兼容 fixture 使用事件回退。两个入口都保留同一个有界、幂等的关闭 Promise。

这项问题是运行实际安装模块后发现的；此前小型 Cordis fixture 的 dispose 事件回归无法证明安装版卸载正确。

## 可重复验收

```powershell
python scripts/accept_installed_harness.py --asar "C:\Users\YOUR_USER\AppData\Local\Programs\DeepSeek Harness\resources\app.asar" --output dist/installed-harness-acceptance.json
```

脚本只读取指定 ASAR 的公开程序模块及其同目录 unpacked 原生依赖，提取到临时目录；不读取 userData、账号、Cookie、会话文件或网络凭据。第三方程序副本不纳入源码与发布包。Node.js 需要满足该安装版的要求，本机使用 Node 24.18.0。

测试使用真实 Cordis、真实 SessionStore/AgentLoop 与 JSONL 后端，供应商部分明确使用确定性本地适配器，执行以下路径：

1. 实际 agent 首个请求前注入单个原生记忆节点。
2. 多轮消息替换自有节点，不无限追加。
3. 实际工具调用、tool result 和下一次模型步骤。
4. Cordis effect 卸载/重载与进程退出监听释放。
5. 实际取消路径提交的文本保存为 interrupted，排除未提交内容。
6. 供应商异常与下一轮恢复。
7. 实际 BasicCompactionEngine 提交压缩，下一轮重建记忆节点；摘要不变成用户事实。
8. 主测试 Node 进程在 flush 后直接退出，跳过插件/框架清理；退出兜底终止自己的 bridge。
9. 新的 Node 进程通过实际 JSONL 持久化取得 OS 写租约并 resume。
10. 回查真实记忆数据库：8 条真实测试用户输入、7 条已投递回答，其中 1 条 interrupted。

2026-10-27 结果为 passed。运行时包版本 `0.2.1-rc.3`，Cordis `4.0.5`。ASAR SHA-256：`04b32e3a872826ba15cfb2081c6a929a8d4ecaa65e09a2543e5a6b30e2eff1f0`。报告记录逐项 checked、版本和 `real_desktop_ui=false` / `live_model=false`，不冒充线上模型或 Electron GUI 的完整产品验收。

## 当前桌面会话的限制

本机 DeepSeek Harness 已运行，沿用应用登录态做真实测试在产品上可行。本次 Codex 工具列表没有 computer-use 技能所需的 `node_repl` / `sky` 控制入口，因此没有操作用户桌面。只读探查本地 API 返回 401；不会提取内部会话 Cookie 或绕过认证。

已提供 [质量评测包导出](quality-acceptance.md)，可由具有桌面控制能力的后续会话在临时 Harness 对话中逐题提交。真实抽取、当前 V4.1 tokenizer/线上 usage 对照、Electron GUI 与用户配置组合验收仍需取得对应执行入口；本次隔离运行时结果只能覆盖上面列出的路径。
