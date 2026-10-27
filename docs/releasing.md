# 安装与开源发布

本地发布工程已完成：MIT 许可证、私密数据忽略规则、隔离安装器、Windows/Linux CI、发布构建工作流及带 SHA-256 的安装包。仓库目前没有 Git remote；远程仓库地址、远程 CI 实跑与正式发布是发布前操作，未通过本轮自动执行。

## 安装

从源码或解压离线发布包后运行：

```powershell
python scripts/install.py --destination "D:\Apps\MemoryDreaming" --user u1 --character c1 --relationship r1
```

安装目录必须为空且位于源码目录之外。安装器创建独立 venv、Python 包、插件副本、数据目录和原生 Cordis 配置；已存在的配置与用户数据不会被覆盖。随后在 Harness 原生插件管理器中选择生成的 `plugin` 绝对路径。安装器不修改现有 Harness profile，也不重启应用。

有 wheel 时可显式传入 `--wheel path/to/memorydreaming-0.29.0-py3-none-any.whl`，使用 `--no-index --no-deps` 安装。离线 ZIP 自带 wheels/，安装器会自动发现。离线包不含 Python、Node、可选模型依赖和模型权重，需预先安装；从纯源码构建 Python 包可能需要联网准备构建工具。

v0.29 的新安装配置默认 `activationMode: command`。安装后在目标窗口发送“记忆开启”；其他窗口不参与。睡眠/苏醒指令详见 [使用说明](window-sleep-wake.md)，长期角色指令和私密迁移见 [角色指南](long-term-role.md)。已有安装目录不可由安装器覆盖，升级可创建新目录并显式选择既有数据库路径，先备份后迁移；旧版程序不能继续写已升级的 schema27 数据库。迁移档案包含原话与私密恢复密钥，应放在忽略目录或仓库之外，不能随开源包发布。

源码插件的默认配置使用 PATH 中的 `python`；跨机器推荐用安装器生成明确的隔离环境路径。API 密钥通过进程环境提供，不写进插件包、数据库快照或示例配置。

## 构建

提交所有拟发布的修改后执行：

```powershell
python -m unittest discover -s tests -q
python scripts/release.py --output dist/release
```

构建脚本拒绝带有未提交已跟踪修改的源码，创建 Python wheel、原生插件 tgz、含已提交源码/安装器/上述包的离线 ZIP、`SHA256SUMS` 和 `release-manifest.json`。它不推送代码、打 tag、上传 PyPI/npm 或发布 GitHub Release。

`.github/workflows/ci.yml` 覆盖 Python 3.10..3.13 的 Linux 和 Python 3.12 的 Windows；`.github/workflows/release.yml` 在显式调度或版本 tag 时执行回归与构建，仅上传工作流 artifact，权限为 contents:read。远程尚未运行，不能据本地验证声称两个平台全部通过。

## 发布前验收

本轮已在包含空格的临时目录创建真实 venv，从 wheel 安装；运行生成的原生插件副本，验证独立 bridge、预取指标、投递补偿和中断回复落库，卸载后正常关闭。完整源码仍需使用用户指定的远程仓库配置 remote，推送后检查 CI，再由维护者决定正式发布。

真实 DeepSeek/语义权重质量、真实 tokenizer 和实际桌面 Harness 生命周期验收保持独立状态，详见 [剩余问题](open-problems.md)。MIT 授权与“已完成全部产品质量验收”是不同事实。
