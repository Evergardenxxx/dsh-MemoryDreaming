# 本地中文语义嵌入与索引一致性

v0.18 增加可选 `SentenceTransformerEmbedder`。默认仍为零依赖字符哈希；启用语义模型需要预先安装依赖并准备本地权重。DeepSeek Harness 继续使用原生 Cordis 插件和 JSONL bridge，记忆注入预算仍为默认 1200、硬上限 4096 token。

## 选择与配置

可以使用兼容 Sentence Transformers 的中文检索模型，例如 [BAAI/bge-small-zh-v1.5](https://huggingface.co/BAAI/bge-small-zh-v1.5)。这是待评估的候选模型，本轮未安装依赖、下载权重或运行真实模型，不能宣称已验证它的中文质量。依赖接口见 [Sentence Transformers API](https://www.sbert.net/docs/package_reference/sentence_transformer/SentenceTransformer.html)。

```powershell
python -m pip install '.[semantic]'
$env:MEMORYDREAMING_EMBEDDING_PROVIDER = 'sentence-transformers'
$env:MEMORYDREAMING_EMBEDDING_MODEL_PATH = 'D:\Models\bge-small-zh-v1.5'
$env:MEMORYDREAMING_EMBEDDING_REVISION = '填写已固定的模型提交 SHA'
$env:MEMORYDREAMING_EMBEDDING_DIM = '512'
$env:MEMORYDREAMING_EMBEDDING_MAX_SEQ_LENGTH = '512'
$env:MEMORYDREAMING_EMBEDDING_BATCH_SIZE = '16'
# 根据所选固定版本模型卡填写；引擎不自动猜测或叠加默认 prompt。
$env:MEMORYDREAMING_EMBEDDING_QUERY_PREFIX = '为这个句子生成表示以用于检索相关文章：'
$env:MEMORYDREAMING_EMBEDDING_DOCUMENT_PREFIX = ''
```

`revision` 是部署者声明的不可变权重版本，不是自动校验本地权重的校验和。更换目录内权重、tokenizer 或配置必须更新它；不要在运行中覆盖模型文件。配置指纹还包含目录位置哈希、维度、查询/文档前缀、最大长度、适配器/投影版本及 L2 归一化策略。移动模型目录也会要求重建。所有写同一记忆库的进程应使用同一套配置。

适配器只读本地目录，使用 CPU、`local_files_only=True`、`trust_remote_code=False`，首次编码才加载模型。缺失依赖或权重不会触发下载。宿主和后台整理器共享一个实例，加载和编码由锁串行保护。查询和文档分别编码，文档支持批量处理；显式空 prompt 避免模型目录自带的默认 prompt 再叠加一次。

原生插件可以直接设置同名驼峰字段，无需改变通信方式：

```yaml
embeddingProvider: sentence-transformers
embeddingModelPath: 'D:\Models\bge-small-zh-v1.5'
embeddingRevision: '填写已固定的模型提交 SHA'
embeddingDim: 512
embeddingQueryPrefix: '为这个句子生成表示以用于检索相关文章：'
embeddingDocumentPrefix: ''
embeddingMaxSeqLength: 512
embeddingBatchSize: 16
```

插件显式字段覆盖继承的 `MEMORYDREAMING_*` 环境变量；未填写的字段保留继承值。`.env.example` 只是清单，引擎不会自动读取 `.env`。宿主只传配置给本地 bridge，不把权重或配置 Schema 放入模型上下文。

## 评估与阈值

```powershell
python -m memorydreaming eval-embedding --k 3 --min-score 0.5 --output embedding-report.json
```

`0.5` 只是阈值试验值，不是推荐值或已校准值。命令使用内存临时库，忽略全局 `--db`，不写用户记忆。报告包含数据/模型配置指纹、Recall@k、MRR@k、Top1、无依据查询误匹配率、分类指标和本机耗时，不包含查询或文档正文。自定义 JSON 可通过 `--dataset` 提供，格式见 [内置数据集](../memorydreaming/data/embedding-zh-v1.json)。

内置合成集有 12 条文档、16 个查询：4 个字面、8 个同义改写和 4 个无依据查询，未经独立人工复核。默认哈希在 `k=3, min_score=0.10` 时字面召回 4/4、改写召回 0/8，总 Recall@3 和 MRR@3 均为 0.333333，无依据误匹配 0/4。这是暴露基线缺口的诊断集，不代表真实中文用户质量。

`quality_complete=true` 仅表示整套评估执行完成，不表示质量达标；CLI 成功退出也不代替验收。加载/编码失败时返回 `status=unavailable, quality_complete=false`、错误代码和退出码 2，不用已完成的前几题给出完整质量指标。

对实际模型应同时比较召回和无依据误匹配，在独立人工标注集上选择阈值，随后配置 `MEMORYDREAMING_VECTOR_MIN_SCORE` 或插件 `vectorMinScore`。默认 0.10 只对哈希基线调过，不能直接当作真实模型的安全阈值。

## 升级与原子重建

v0.18 将完整配置指纹写入 `memory_vectors.model`；旧版只有模型名的投影会跳过并提示不完整。升级或换配置后，以与插件相同的环境执行：

```powershell
python -m memorydreaming --db .memorydreaming/deepseek-harness.db reindex --user u1 --character c1 --relationship r1 --domain real
```

不传 `--relationship` 会覆盖该角色在所选领域的全部关系；可用 `--activity` 进一步收窄。建议暂时停用同库后台整理，以减少重建的代次冲突。

1. 短事务读取作用域内有效且未抑制的记忆和全库内容代次。
2. 释放事务，在事务外批量编码正文。
3. 开短事务重新检查代次；一致才原子替换该作用域的投影，否则抛 `SourceRevisionChanged`，由调用者重新运行。

编码失败和发布失败不会先删除原索引；计算期间发生修改、遗忘或其他索引更新，旧计算不得写回。全库代次对无关作用域变更也会保守冲突。正文通过仓库 API 更新时会清除旧向量；调用方随后重算或重建，不能用旧正文投影继续匹配。直接 SQL 修改不受此 API 保证，应显式重建。

表仍为每条记忆一份向量投影，不支持多个模型并存。不同配置的索引不会混用，但并发使用不同配置写同库会互相替换投影。

## 降级与边界

维度不匹配、非有限数字及真实模型的零向量视为编码失败；读取时跳过损坏 BLOB、错维度和零向量，保留其余有效命中。关键词、实体和时间通道继续执行。SDK 的 `degraded_reasons`、LLM 工具的 `partial / retrieval_complete=false / warnings` 和逐轮恢复 payload 都保留降级信息：

| 代码 | 含义 |
|---|---|
| `EMBEDDING_UNAVAILABLE` | 依赖、目录、模型加载或编码不可用 |
| `INVALID_EMBEDDING` | 模型维度/长度或输出数值不合法 |
| `VECTOR_INDEX_CORRUPT` | 可见范围内存在损坏向量 |
| `VECTOR_INDEX_INCOMPLETE` | 可见范围内存在其他配置投影；真实模型还检查完全缺失的投影 |

降级结果不进入本地或跨进程候选缓存，模型恢复后同一查询可以重新计算。`prefetch` 降级时返回 `cached=false`。默认哈希保留手工插入后显式建索引的兼容行为，不将完全未建投影单独报告为降级；已有旧版/错配投影仍会报告。

所有候选仍经过作用域、活动授权、来源、状态和事实抑制复核。双时间历史查询不使用当前向量，继续只读修订快照。新适配器没有改变核心审批或遗忘权限。

尚未完成：真实模型质量和依赖安装验收、大规模耗时/内存验证、ANN 索引。v0.19 将后台抽取/去重编码移出写事务，宿主查询改为只读快照并在发布时复核代次，见 [事务外计算](optimistic-computation.md)。v0.20 已将同步纠错及结构化审批替换的编码移出事务，加入提交前复核和原子回执，见 [纠错与变更审批](correction-approval.md)。当前本地编码没有可强制打断的硬超时，不能把故障回退称为超时保证。

## v0.18 工程验证

412 项 unittest、compileall、原生插件 Node 语法检查及 diff 检查通过。新增回归覆盖依赖缺失、前缀区分、配置隔离、非法数值、索引损坏、写入失败回滚、重建期间跨连接修改和遗忘，以及降级不缓存后的恢复。Node 模拟 Cordis 事件并启动真实 Python bridge，验证模型配置透传及降级；这不等于真实 Harness 主程序验收。

实际降级响应通过 JSON Schema 校验。v0.18 wheel 已在独立临时目录安装并运行内置评估，44 个源码/数据文件与仓库一致，可选依赖元数据存在。上述验证均未加载真实语义模型，不能证明真实模型依赖组合、语义质量或性能已通过验收。


v0.26 Runtime 默认以独立进程运行本地 SentenceTransformer，MEMORYDREAMING_EMBEDDING_TIMEOUT_SECONDS 默认 30 秒，超时终止并在下次调用重启。直接构造底层适配器或注入自定义 Embedder 仍由可信调用方负责终止语义；模型权重质量仍未验收。详见 [运维与恢复](operations-and-recovery.md)。
