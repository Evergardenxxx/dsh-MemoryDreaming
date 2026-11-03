"""已核对的本地模型部署约定；不导入模型库、不下载或探测权重。

Profile 只描述编码协议，不证明本地文件来自作者，也不提供质量阈值。
查询指令、实际维数和投影算法进入适配器指纹；换配置必须重建索引。
"""

from __future__ import annotations

from types import MappingProxyType


QWEN3_NATIVE_DIMS = MappingProxyType({
    "qwen3-embedding-0.6b": 1024,
    "qwen3-embedding-4b": 2560,
    "qwen3-embedding-8b": 4096,
})
EMBEDDING_PROFILES = ("generic", *QWEN3_NATIVE_DIMS)

# 采用官方 Instruct / Query 格式，任务内容为本项目的记忆检索定义。
# 这不是官方 web-search 任务，阈值和效果必须单独评测。
QWEN3_MEMORY_QUERY_PREFIX = (
    "Instruct: Given a conversation query, retrieve relevant memories that help answer the query\nQuery:"
)


def validate_embedding_profile(*, provider: str, profile: str, dim: int,
                               query_prefix: str, document_prefix: str) -> None:
    """在进程或数据库打开之前拒绝不完整的部署配置。"""
    if not isinstance(profile, str) or profile not in EMBEDDING_PROFILES:
        raise ValueError("embedding_profile 不是受支持的本地模型配置")
    if profile == "generic":
        return
    if provider != "sentence-transformers":
        raise ValueError("Qwen3 profile 需要 sentence-transformers 本地嵌入后端")
    if not 32 <= dim <= QWEN3_NATIVE_DIMS[profile]:
        raise ValueError("Qwen3 输出维度必须为 32..所选模型原生维度")
    if document_prefix:
        raise ValueError("Qwen3 记忆正文不应附加查询指令，document_prefix 必须为空")
    if query_prefix and (not query_prefix.startswith("Instruct: ")
                         or not query_prefix.endswith("\nQuery:")
                         or not query_prefix[len("Instruct: "):-len("\nQuery:")].strip()):
        raise ValueError("Qwen3 自定义查询前缀必须为 Instruct: 任务描述\\nQuery:")


def sentence_transformer_options(settings) -> dict:
    """运行时、离线评测使用相同编码约定，避免评测后部署时漏加指令。"""
    options = {
        "revision": settings.embedding_revision,
        "dim": settings.embedding_dim,
        "query_prefix": settings.embedding_query_prefix,
        "document_prefix": settings.embedding_document_prefix,
        "max_seq_length": settings.embedding_max_seq_length,
        "batch_size": settings.embedding_batch_size,
    }
    if settings.embedding_profile != "generic":
        options.update({
            "query_prefix": settings.embedding_query_prefix or QWEN3_MEMORY_QUERY_PREFIX,
            "matryoshka": True,
            "expected_model_dim": QWEN3_NATIVE_DIMS[settings.embedding_profile],
            "model_profile": "qwen3",
        })
    return options
