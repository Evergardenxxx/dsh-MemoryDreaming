# MemoryDreaming for DeepSeek Harness

这是一个配置型 DeepSeek Harness bundle，不包含运行时代码。它把已经安装的 `@deepseek-ai/dsh-mcp-client` 连接到仓库内的 MemoryDreaming MCP stdio 服务。

## Bundle 内容

- `cordis.patch.yml`：注册 `memorydreaming` MCP server。
- `locale/en.json`、`locale/zh.json`：插件管理器显示名称和描述。
- `icon.svg`：插件图标。

服务使用 `C:\Python314\python.exe`、仓库根目录作为 `cwd`，并把身份绑定为 `u1 / c1 / r1`。数据库位于仓库的 `.memorydreaming/deepseek-harness.db`，不会写入 Harness profile 目录。

## 安装

在 DeepSeek Harness 的插件管理器中安装本目录的绝对路径。安装后重载或重启应用，确认出现：

- `mcp__memorydreaming__memory_search`
- `mcp__memorydreaming__memory_read`

首次连接会自动创建数据库文件。此 bundle 只启用只读工具；写入、整理和核心审批仍由可信宿主的 `HostAdapter` 生命周期接口负责。
