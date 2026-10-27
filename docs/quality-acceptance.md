# 真实模型质量验收与本轮结果

v0.27 的质量入口把“模型执行完成”与“质量验收通过”明确分开。真实中文嵌入权重已在本机执行；没有独立人工标注和足够数量的验收样例，报告保持 `quality_accepted=false`。本轮未调用付费 DeepSeek API，也没有从用户应用读取密钥。

## 已执行的真实权重

模型为 BAAI 的 BGE small Chinese v1.5，从 ModelScope 公开镜像 `AI-ModelScope/bge-small-zh-v1.5` 下载。固定 revision：`b7b841ce366cce9454e10934fd8ad91dd4e5913d`。仅下载 safetensors、标准配置和 tokenizer 共 10 个文件、96,378,296 字节；逐文件核对远端 SHA-256。权重文件 SHA-256 为 `354763b9b1357bc9c44f62c6be2276321081ed2567773608c0d0785b61d5a026`。不用 pickle 权重，不启用 remote code，不改动全局 Python 环境。官方 Hugging Face 和 hf-mirror 请求超时，因此使用公开 ModelScope 镜像，镜像来源不等同作者直接发布验证。

本机 `dist/quality-venv` 使用 Python 3.14、PyTorch 2.14 CPU、sentence-transformers 5.7.0、transformers 5.21.0、tokenizers 0.23.3。权重位于忽略的 `dist/models/bge-small-zh-v1.5`，不会放入源码包或用户记忆库。完整下载摘要保存在该目录的 `download-manifest.json`。

实际使用项目的 `SentenceTransformerEmbedder`，512 维、L2 归一、最多 512 token；query prefix 为 `为这个句子生成表示以用于检索相关文章：`，文档无前缀。

| 真实权重诊断 | 正例 Recall@3 / MRR / Top1 | 改写 Recall@3 | 负例误匹配 |
|---|---|---|---|
| 内置 12 文档、16 查询，阈值 0.50 | 12/12，三项均 1.0 | 8/8 | 0/4 |
| 8 题校准后冻结阈值 0.50，在另一组 8 题验收 | 6/6，三项均 1.0 | 4/4 | 0/2 |

第二组仅在 calibration 的 8 题上比较 0.30、0.40、0.45、0.50、0.55、0.60、0.65、0.70、0.75、0.80；evaluation 的查询 ID 与归一化正文均不重叠，标签不用于选阈值。冻结 artifact SHA-256 为 `495fc5ab0ded5120b0c846ffabc8cab344a6ec629cec9fd13fc54307fbd554c4`。全部数据仍来自原内置合成诊断集，分割不使它自动成为独立人工标注集，也不能代表真实陪伴对话分布。

本轮验收报告只剩两个拒绝原因：`INDEPENDENT_HUMAN_REVIEW_REQUIRED`、`INSUFFICIENT_EVALUATION_SAMPLES`。结果保存在 `dist/quality-evaluation/bge-heldout-report.json`；全量诊断为 `dist/bge-small-zh-real-baseline.json`。这已经证明适配器可执行真实权重和召回这些改写，不能据此宣称真实语言质量全面验收。

真实权重规模检查也已执行：10,000 条合成短情景、512 维、CPU 2 线程、batch_size=32；写入约 1.78 秒，包含模型加载的向量构建约 44.87 秒，8 次未缓存精确向量查询中位 443.22 ms、最大 597.64 ms。报告为 `dist/quality-evaluation/bge-real-scale-10000.json`。这是真实编码和检索的工程规模基线，所有数据仍为合成，不能替代长期用户质量或更大规模的验收；并发负载与硬件会改变耗时。

## 可重复执行

```powershell
python -m venv --system-site-packages dist/quality-venv
dist/quality-venv/Scripts/python.exe -m pip install "sentence-transformers>=3.4,<6"
python scripts/prepare_quality_model.py --output dist/models/bge-small-zh-v1.5

# 只做诊断切分，不创造人工标签；正式验收应提供独立数据文件。
python scripts/quality_acceptance.py split-diagnostic --kind embedding --dataset memorydreaming/data/embedding-zh-v1.json --output dist/quality-evaluation

dist/quality-venv/Scripts/python.exe scripts/quality_acceptance.py embedding --model-path dist/models/bge-small-zh-v1.5 --revision modelscope-b7b841ce366cce9454e10934fd8ad91dd4e5913d --calibration dist/quality-evaluation/calibration.json --evaluation dist/quality-evaluation/evaluation.json --calibration-artifact dist/quality-evaluation/frozen.json --output dist/quality-evaluation/report.json

dist/quality-venv/Scripts/python.exe scripts/benchmark_semantic.py --model-path dist/models/bge-small-zh-v1.5 --revision modelscope-b7b841ce366cce9454e10934fd8ad91dd4e5913d --size 10000 --output dist/quality-evaluation/scale.json
```

Linux/macOS 将 venv Python 路径改为 `dist/quality-venv/bin/python`。上例默认阈值网格是 0.10..0.90、步长 0.05；本轮表格中的十点网格通过 SDK 显式传入。数值能复算，但耗时与不同依赖版本可能变化。生产启用同一模型时必须同时使用对应本地路径、不可变 revision、query prefix 和经验证阈值；不会擅自改用户当前插件配置。

`calibrate_embedding` 只能对 calibration 评分；输出文件包含模型配置摘要、两份数据摘要、阈值网格、选中阈值、k 和预先声明的政策。`evaluate_embedding_acceptance` 复核冻结文件，拒绝篡改、模型更换、数据变更及样例交叉污染，不会在最终评估中重新找更好阈值。阈值不能为弥补验收失败而反复重调后仍声称 held-out。

默认正式验收政策为至少 100 题、50 正例、50 负例，Recall≥0.80、改写 Recall≥0.75、抽取 Precision≥0.95、负例错记/匹配率≤0.02。政策可由可信调用方在校准前构造 `QualityPolicy`，不是目标模型准确率承诺。报告提供单独拒绝码，不把执行失败从分母删掉，也不把空输出当作高精度证明。

独立人工复核记录由项目外的实际审查者提供，通过 `--review` 传入；不能让模型给自己签名：

```json
{
  "independent_human_review": true,
  "reviewer": "实际评审者标识",
  "reviewed_at": "2026-10-27T20:00:00+08:00",
  "calibration_dataset_sha256": "实际校准集SHA256",
  "evaluation_dataset_sha256": "实际验收集SHA256"
}
```

程序只验证字段、时区与数据绑定，不能认证现实中的评审身份；这是可信宿主输入。hash baseline、离线 replay 与注入 mock 不属于正式真实模型证据。原有 `quality_complete` 为向后兼容继续表示一次向量评估执行完成，新增 `execution_complete` 和明确的 `quality_accepted`；只有严格验收入口可把后者设为 true。

## 使用已登录 DeepSeek Harness 评估

已有软件登录不等于当前进程获得 API 凭据，也不能绕过应用认证。提供无密钥便携路径，供实际宿主发起请求并保留原始返回：

```powershell
python scripts/quality_acceptance.py export-harness --dataset memorydreaming/data/extraction-zh-v1.json --output dist/harness-extraction-eval
```

输出逐题 prompt、固定 schema、数据摘要 manifest 和 `predictions.json` 模板。prompt 只含事件资料和抽取契约，不泄露答案标签。未运行的题明确写 `NOT_RUN`，不是空候选成功；非用户事件沿用生产本地跳过模型的策略。实际在专用临时对话逐题提交 prompt，把返回的 JSON 原样放入相应 payload，记录实际模型及软件版本。不要引入私人会话历史，不要手工修改候选以匹配答案。

```powershell
python scripts/quality_acceptance.py extraction-replay --calibration path/to/independent-calibration.json --evaluation path/to/independent-evaluation.json --predictions path/to/predictions.json --output dist/extraction-replay-report.json
```

replay 通过生产严格解析器校验候选和 source_span，评分可复核；手工重放本身不能证明请求来自哪个真实模型，因此正式 `REAL_MODEL_EVIDENCE_REQUIRED` 仍保留。若可信环境配置 `DEEPSEEK_API_KEY`，可显式运行同脚本的 `extraction-deepseek` 分支；没有 key 时生成 `needs_credentials` 报告、`provider_called=false` 并返回 2，不搜索凭据或自动付费。

本轮导出了分割后的 12 题到 `dist/harness-extraction-eval`，未假装已经在桌面软件执行。当前桌面实测的执行途径由宿主集成负责；真实 DeepSeek 候选和独立人工标签仍是完成最终质量验收所需的外部证据。

主运行时的独立模型进程也已用该真实 BGE 权重验证：保存“我喜欢睡前听海边故事。”后，查询“入睡之前想听有关大海的童话”返回对应证据；模型 worker 实际启动且 Runtime.close 确认它退出。加载、抽取编码和查询总计约 12.85 秒，结果在 `dist/quality-evaluation/isolated-runtime-real-model.json`。这项验证覆盖进程适配与回源，不扩大质量样本量。
