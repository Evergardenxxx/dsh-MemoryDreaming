# 本地 DeepSeek tokenizer 与请求投影

已实现本地 tokenizer 的显式接入，区分三种能力：

| 能力 | 计量范围 | 声明 |
|---|---|---|
| `LocalDeepSeekTokenizer.count(text)` | 固定 tokenizer 对一段字符串编码 | `exact`，只针对该字符串 |
| `count_template(messages)` | 固定 tokenizer_config 的本地 Jinja chat template | `exact`，只针对该本地模板，不等于线上用量 |
| 原生 `LocalDeepSeekRequestCounter` | 先投影 Harness 的文本 Messages 请求，再用固定 tokenizer 编码 wire JSON | `estimated`；`text_tokenization=exact`、`template_fidelity=wire-json-estimate` |

远程 Messages 服务的内部模板和当前模型 tokenizer 未公开验证时，不能把真实分词包装成“完整请求精确计数”。当前安装版 Harness 的默认模型是 `deepseek-flash` / DeepSeek-V4.1，窗口声明为 1,048,576；公开 DeepSeek-V3 tokenizer 不是该模型的替代品。模型名、窗口和资产版本必须独立核验。

## 本地资产与绑定

把可信公开模型的 `tokenizer.json` 和 `tokenizer_config.json` 放入独立目录。无需模型权重，运行时不会下载文件或执行模型 Python 代码。选定的 Python 环境需安装可选 `tokenizers`，本地模板计数再需要 `jinja2`。

显式建立校验清单：

```powershell
python -m memorydreaming.tokenization --directory D:\Models\deepseek-v3-tokenizer --create-manifest --provider deepseek --model deepseek-v3-pinned-local --revision <immutable-asset-revision>
```

生成的 `memorydreaming-tokenizer.json` 固定格式版本、明确 provider IDs / model IDs、tokenizer/config SHA-256 和 revision，禁止覆盖原清单。此操作只是可信维护者确认资产映射，不会自动证明动态别名 `deepseek-chat` 仍对应这份权重；应只声明已经核验的固定模型 ID。

每次 Python 实际计数校验哈希；缺资产、损坏、缺依赖或模型不匹配明确失败，不静默切回其他 tokenizer。Node 的 64 项有界结果缓存只保存输入摘要和计数，资产 size/mtime/ctime 变化后重算。原文只通过本地子进程 stdin 传递，不放在命令行、日志、模型提示或持久计量表中。

## 原生插件配置

可信插件配置可设置：

```json
{
  "localTokenizer": {
    "directory": "D:\\Models\\deepseek-v3-tokenizer",
    "pythonCommand": "D:\\Apps\\MemoryDreaming\\venv\\Scripts\\python.exe",
    "timeoutMs": 5000,
    "inHistory": true
  }
}
```

`configureLocalTokenizer(config)` 创建同步请求计数器，供现有预览和最终发送前预算检查使用；不能与另一个 `requestTokenCounter` 同时配置。插件的 `pythonCommand` 可作为默认值。每次未命中计量缓存会启动受 `timeoutMs` 限制的只读本地进程；默认 5 秒，范围 100..30000 ms；输入最多 16 MiB、计量响应最多 64 KiB。超时或不支持输入会使用现有预算失败/跳过记忆注入的明确路径。

预算报告的 `precision` 仅包含固定的分词模式、模板可信度、投影名和资产指纹，不包含原文：

```json
{
  "count_mode": "estimated",
  "precision": {
    "text_tokenization": "exact",
    "template_fidelity": "wire-json-estimate",
    "projection": "deepseek-messages-text-v1",
    "tokenizer_revision": "pinned-revision"
  }
}
```

当前公共 Messages 服务模板未核验，因此 `safetyMargin` / `requestOverhead` 仍保留，不能据此声称消除了超限风险。已签发完整 request 计数回调接口依旧可由真正拥有供应商模板的可信宿主实现 `exact`。

## 投影范围

文本投影与已安装 `@deepseek-ai/dsh-llm-deepseek` 的 `serialize` 规则对齐，覆盖系统文本、普通 user/assistant 文本、无签名 thinking、tool use/result、同角色合并、tool schema、defer loading、in-history system 更新，以及明确的 developer tool addition/removal。未完成工具调用、重复 call ID、未知输入字段都会拒绝。

文件/图像/附件、签名原生 replay、尚未展开的 toolHistory updates 和任意扩展块要求供应商拥有的最终投影，因此当前显式返回 `TOKENIZER_PROJECTION_UNSUPPORTED`。它不会把二进制/base64 字节数或文件引用 JSON 当作真实视觉 token。`inHistory` 必须和选定模型路由能力一致；默认 true 对应当前安装版的默认路由。

Python CLI 可单独验证文本或本地模板：

```powershell
'{"kind":"text","text":"爸爸，我还记得海边的灯塔。"}' | python -X utf8 -m memorydreaming.tokenization --directory D:\Models\deepseek-v3-tokenizer
```

## 本机验证（2026-10-27）

从公开的 `modelscope.cn/models/deepseek-ai/DeepSeek-V3` 获取 tokenizer 小文件，未下载模型权重、未调用 API、未读取密钥：

- `tokenizer.json`：7,847,652 字节，SHA-256 `621ac2e32d0dba658404412318818aaa8ce8cda492e59830109d8da6b517fb41`。
- `tokenizer_config.json`：3,128 字节，SHA-256 `637bcd1a08cf7c772ce6a383196b22930921c79c3c73223c5afc0c7f41545546`。
- 使用本地 `tokenizers 0.23.3`，`爸爸，我还记得海边的灯塔。` 为 8 tokens，`Hello, MemoryDreaming!` 为 6 tokens，`😀 我喜欢猫。` 为 6 tokens。
- 本地官方 chat template 的“system=你是小梦。 / user=爸爸喜欢什么故事？”为 12 tokens；这只是公开 V3 模板结果。
- 4 个 native 文本/工具投影用例与已安装 Harness 的实际序列化函数输出逐项一致，wire JSON 经真实 tokenizer 计数为 43 / 153 / 73 / 79。适配器源码 SHA-256：`21fedb71f8d9aafea73b35ee13bc73737a8271d7ffa261697d4208e8563196cb`。

下载资产、依赖和本机验证结果仅保留在已忽略的 `dist/`，不把第三方 tokenizer 或安装包源码加入项目。8 项专项回归与 5 项原预算回归通过；测试 fixture 验证接口边界，真实资产结果另行记录。当前 V4.1 同版本 tokenizer 与线上 usage parity 仍未取得，不能因此关闭它的产品验收项。
