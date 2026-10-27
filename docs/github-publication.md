# GitHub首次发布准备

当前源码可作为试用版开源。以下区分已经具备的仓库材料与推送后才能完成的外部操作；不把本地工作流配置当作远程CI通过证据。

2026-10-27本地准备已验证738项工程回归、跨窗口示例、首次设定CLI、文档内部链接、YAML与SVG结构，并实际渲染检查架构图。当前候选文件和已有历史进行了常见凭据格式扫描，历史文件清单未发现数据库/默认私密目录；这类启发式检查不等于能识别任意私人对话或未知密钥格式。

## 仓库材料

| 文件/目录 | 用途 | 状态 |
|---|---|---|
| `README.md` / `README.en.md` | 项目目的、试用状态、架构图、上手与能力边界 | 已准备 |
| `docs/assets/architecture.svg` / `docs/diagrams/architecture.mmd` | 可展示与可编辑的系统架构图 | 已准备 |
| `docs/architecture-overview.md` | 普通回合、睡眠/苏醒流程与系统边界 | 已准备 |
| `docs/quickstart.md` / `docs/README.md` | 安装、首次角色设定、指令与文档导航 | 已准备 |
| `examples/initialize_role.py` | 有实际来源、拒绝覆盖旧值的首次设定示例 | 已准备 |
| `ROADMAP.md` / `CHANGELOG.md` | 未完工作、工程证据与版本变化 | 已准备 |
| `LICENSE` / `CONTRIBUTING.md` / `SECURITY.md` | MIT、贡献要求与私密问题反馈 | 已准备 |
| `.github/ISSUE_TEMPLATE/` / PR模板 | 最小复现、功能建议、验证范围 | 已准备 |
| `.github/workflows/` | CI矩阵与本地附件构建工作流 | 已配置，远程待跑 |
| `.env.example` / `.gitignore` / `.gitattributes` | 配置说明、私密文件忽略、跨平台文本格式 | 已准备 |
| `pyproject.toml` / 原生`package.json` | 引擎、插件与打包元数据 | 已准备 |

## 上传什么

上传已审查并提交的源码、测试、合成评估数据、文档和配置模板；让Git读取忽略规则。不要直接把整个工作目录拖进网页上传：工作区中可能有本地数据库、API环境、Harness运行时副本、模型权重、私密档案和构建缓存。

`dist/`里的wheel、tgz、离线ZIP、`SHA256SUMS`及manifest作为Release附件，不提交到源码树。安装包不含模型和Python/Node运行环境。任意命名的快照/备份不会全部被扩展名规则自动识别，建议始终存放在`.memorydreaming/`或仓库外。

`.gitignore`只约束未跟踪文件，不会撤销旧提交。首次公开前审查当前索引和完整历史，确认没有私密原话、密钥、数据库、恢复材料或用户profile。若实际凭据曾进入历史，先撤销凭据，再处理历史；不能只补一条忽略规则。

## 本地确认

```bash
git status --short
git diff --check
git ls-files
python -X utf8 examples/host_lifecycle.py
python -m unittest discover -s tests -q
```

提交拟发布文件后构建：

```bash
python scripts/release.py --output dist/github-release
```

检查`release-manifest.json`和`SHA256SUMS`；离线ZIP应包含已提交源码与安装器、wheel和原生tgz，不包含`.memorydreaming/`、运行时profile或本机模型权重。图和文档应能在GitHub中直接查看，内部链接、示例参数和版本号保持一致。

## GitHub上的操作

1. 创建名为`MemoryDreaming`的空仓库；本地已有README/LICENSE时不要让网站另建同名初始文件。
2. 仓库描述可用：`Local long-term memory for companion agents, with native DeepSeek Harness integration, source tracking, and sleep/wake continuity.`
3. Topics可用：`long-term-memory`、`llm`、`ai-agents`、`deepseek`、`sqlite`、`roleplay`。
4. 配置真实remote后推送`main`，检查GitHub Actions的Linux/Windows测试与构建结果。
5. 开启Issues；如账户/仓库支持，可开启私密漏洞报告。保护规则在CI首次生成实际check名称后配置。
6. 需要发布可下载包时，确认本地版本与tag一致，使用预发布说明并上传构建附件。

项目仓库为[Evergardenxxx/dsh-MemoryDreaming](https://github.com/Evergardenxxx/dsh-MemoryDreaming)，本地origin已绑定相同HTTPS地址。首页CI badge连接实际工作流；具体运行结果以[Actions](https://github.com/Evergardenxxx/dsh-MemoryDreaming/actions)为准。源码推送、tag和Release是独立操作，版本记录不表示已发布下载附件。

## 首个Release说明建议

标题可用`v0.29.0 — Experimental long-term memory plugin`，标记为Pre-release，并说明：

- 支持原生按窗启用、跨窗口持久恢复、睡眠/苏醒和七项角色连续性能力。
- 默认规则抽取覆盖有限；可选DeepSeekAPI/本地语义模型单独配置。
- 主动提醒只有本地队列，尚无定时推送渠道。
- 本地工程回归/隔离安装已通过；真实桌面/登录模型、当前V4.1计量、独立人工质量和长期角色质量仍待验收。
- 安装步骤、升级时先备份数据库，以及MIT许可。

实际Release正文应列本次构建版本与附件，不能把这份模板当成已经发布的记录。
