"""运行配置。

所有可调参数都集中在这里，并且可以从环境变量覆盖。
新手最容易踩的坑是「魔法数字散落在各处」——所以像 48 小时窗口、候选预算、
token 预算这种架构文档里点名的参数，一律先在这里声明，再由各模块读取。
"""

from __future__ import annotations

import os
import math
from dataclasses import dataclass, field, replace
from pathlib import Path

ENV_PREFIX = "MEMORYDREAMING_"

# 默认数据库放在项目目录下的隐藏文件夹，.gitignore 已经忽略它
DEFAULT_DB_PATH = Path(".memorydreaming") / "memory.db"


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(ENV_PREFIX + name)
    return raw if raw else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(ENV_PREFIX + name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        # 配置写错时不要悄悄用默认值，否则问题很难查。这里明确报警后回退。
        print(f"[memorydreaming] 环境变量 {ENV_PREFIX}{name}={raw!r} 不是整数，回退为 {default}")
        return default


@dataclass(frozen=True)
class DeepSeekSchedulingPolicy:
    """同库、同端点、同凭据的请求配额；不进入模型上下文。"""

    max_concurrent: int = 1
    min_interval_seconds: float = 0.0
    cooldown_seconds: float = 5.0
    lease_seconds: float = 120.0
    quota_db_path: str = ""
    requests_per_minute: int = 0
    tokens_per_minute: int = 0

    def __post_init__(self):
        if not isinstance(self.quota_db_path, str):
            raise ValueError("quota_db_path 必须为路径字符串")
        for name in ("requests_per_minute", "tokens_per_minute"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100000000:
                raise ValueError(f"{name} 必须为非负整数")
        if (isinstance(self.max_concurrent, bool) or not isinstance(self.max_concurrent, int)
                or not 1 <= self.max_concurrent <= 32):
            raise ValueError("DeepSeek max_concurrent 必须在 1..32 内")
        for name, minimum, maximum in (("min_interval_seconds", 0, 3600),
                                       ("cooldown_seconds", 0.1, 86400),
                                       ("lease_seconds", 1, 3600)):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not minimum <= value <= maximum):
                raise ValueError(f"DeepSeek {name} 必须在 {minimum}..{maximum} 内")

    @classmethod
    def from_env(cls):
        try:
            return cls(
                max_concurrent=int(_env_str("DEEPSEEK_MAX_CONCURRENT", "1")),
                min_interval_seconds=float(_env_str("DEEPSEEK_MIN_INTERVAL_SECONDS", "0")),
                cooldown_seconds=float(_env_str("DEEPSEEK_COOLDOWN_SECONDS", "5")),
                lease_seconds=float(_env_str("DEEPSEEK_REQUEST_LEASE_SECONDS", "120")),
                quota_db_path=_env_str("DEEPSEEK_QUOTA_DB", ""),
                requests_per_minute=_env_int("DEEPSEEK_RPM", 0),
                tokens_per_minute=_env_int("DEEPSEEK_TPM", 0),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("DeepSeek 调度配置无效") from exc


@dataclass(frozen=True)
class Settings:
    """引擎配置。frozen=True 表示创建后不可改，避免运行时被意外篡改。

    需要不同配置时用 `settings.with_db(...)` 之类的辅助方法生成新对象。
    """

    # 数据库文件路径
    db_path: Path = field(default_factory=lambda: DEFAULT_DB_PATH)

    # 默认时区：解析「昨天」「今天」等相对时间时使用
    timezone: str = "Asia/Shanghai"

    # 架构文档第 5 节：最近 48 小时作为「默认检索粒度」的可配置边界。
    # 注意它影响的是展开粒度，不是禁止旧事精确检索。
    recent_detail_window_hours: int = 48

    # 单次检索最终返回的记忆条数上限
    max_context_items: int = 12

    # 组装给模型的上下文 token 预算（保守估算，不是精确分词）
    context_token_budget: int = 1800

    # 高相关类型每类的候选预算；低相关类型只留小额探查预算
    per_type_candidate_budget: int = 8
    probe_candidate_budget: int = 2

    # RRF（倒数排名融合）的平滑常数，取 60 是信息检索里的常见默认值
    rrf_k: int = 60

    # 可用性衰减：60 天半衰期 + 0.3 下限（见 retrieval/decay.py）
    decay_half_life_hours: int = 24 * 60
    decay_floor: float = 0.3

    # 单次关键词检索最多取多少条候选
    keyword_candidate_limit: int = 40

    # 向量通道：候选条数与相似度下限。
    # 下限不能省：任何两句中文都会共享一些二字词，没有阈值就全是噪声。
    # 也不能定太高——短查询和长记忆的余弦天然偏低。
    # 0.10 是实测出来的分界线：1024 维哈希嵌入下，无关问句稳定是 0.0，
    # 有明显字面重合的在 0.10~0.45。换嵌入模型后必须重新标定。
    vector_candidate_limit: int = 20
    vector_min_score: float = 0.10

    # 真实语义嵌入为显式可选项；只能使用预先下载的本地权重。
    embedding_provider: str = "hash"
    embedding_model_path: str = ""
    embedding_revision: str = ""
    embedding_dim: int = 512
    embedding_query_prefix: str = ""
    embedding_document_prefix: str = ""
    embedding_max_seq_length: int = 512
    embedding_batch_size: int = 16
    embedding_timeout_seconds: float = 30.0

    # bootstrap 时附带多少条「重要经历」摘要
    bootstrap_highlight_limit: int = 5

    # 巩固任务：每次领取独立租约，失败延迟重试；上限在入队时持久保存。
    job_lease_seconds: int = 120
    job_max_attempts: int = 3
    job_retry_base_seconds: int = 5
    job_retry_max_seconds: int = 300

    # 后台整理器默认关闭，便于批处理、单元测试和显式控制。
    # 原生聊天插件应显式打开；它会使用独立数据库连接，不阻塞对话请求。
    auto_consolidation: bool = False
    consolidation_interval_seconds: float = 1.0
    consolidation_batch_size: int = 4

    def __post_init__(self) -> None:
        if (isinstance(self.embedding_timeout_seconds, bool) or not isinstance(self.embedding_timeout_seconds, (int, float))
                or not math.isfinite(self.embedding_timeout_seconds) or not 0.1 <= self.embedding_timeout_seconds <= 300):
            raise ValueError("embedding_timeout_seconds 必须为 0.1..300")
        if self.embedding_provider not in ("hash", "sentence-transformers"):
            raise ValueError("embedding_provider 必须为 hash 或 sentence-transformers")
        if self.embedding_provider == "sentence-transformers" and (
            not isinstance(self.embedding_model_path, str) or not self.embedding_model_path.strip()
            or not isinstance(self.embedding_revision, str) or not self.embedding_revision.strip()
        ):
            raise ValueError("真实嵌入必须显式配置本地模型路径和不可变 revision")
        for name, maximum in (("embedding_dim", 65536), ("embedding_max_seq_length", 32768),
                              ("embedding_batch_size", 1024)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"{name} 超出有效范围")
        for prefix in (self.embedding_query_prefix, self.embedding_document_prefix):
            if not isinstance(prefix, str) or len(prefix) > 2000:
                raise ValueError("嵌入前缀必须为不超过 2000 字符的字符串")
        if (isinstance(self.vector_min_score, bool) or not isinstance(self.vector_min_score, (int, float))
                or not math.isfinite(self.vector_min_score) or not -1 <= self.vector_min_score <= 1):
            raise ValueError("vector_min_score 必须在 [-1, 1] 内")
        for name in ("job_lease_seconds", "job_max_attempts", "job_retry_base_seconds", "job_retry_max_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} 必须为正整数")
        if self.job_retry_max_seconds < self.job_retry_base_seconds:
            raise ValueError("job_retry_max_seconds 不能小于 job_retry_base_seconds")
        if not isinstance(self.auto_consolidation, bool):
            raise ValueError("auto_consolidation 必须为布尔值")
        if (isinstance(self.consolidation_interval_seconds, bool)
                or not isinstance(self.consolidation_interval_seconds, (int, float))
                or self.consolidation_interval_seconds <= 0):
            raise ValueError("consolidation_interval_seconds 必须为正数")
        if (isinstance(self.consolidation_batch_size, bool)
                or not isinstance(self.consolidation_batch_size, int)
                or self.consolidation_batch_size < 1):
            raise ValueError("consolidation_batch_size 必须为正整数")

    @classmethod
    def from_env(cls) -> "Settings":
        """从环境变量构造配置，缺省项用上面的默认值。"""
        return cls(
            db_path=Path(_env_str("DB", str(DEFAULT_DB_PATH))),
            timezone=_env_str("TIMEZONE", "Asia/Shanghai"),
            recent_detail_window_hours=_env_int("RECENT_WINDOW_HOURS", 48),
            max_context_items=_env_int("MAX_ITEMS", 12),
            context_token_budget=_env_int("TOKEN_BUDGET", 1800),
            vector_min_score=float(_env_str("VECTOR_MIN_SCORE", "0.10")),
            embedding_provider=_env_str("EMBEDDING_PROVIDER", "hash"),
            embedding_model_path=_env_str("EMBEDDING_MODEL_PATH", ""),
            embedding_revision=_env_str("EMBEDDING_REVISION", ""),
            embedding_dim=_env_int("EMBEDDING_DIM", 512),
            embedding_query_prefix=_env_str("EMBEDDING_QUERY_PREFIX", ""),
            embedding_document_prefix=_env_str("EMBEDDING_DOCUMENT_PREFIX", ""),
            embedding_max_seq_length=_env_int("EMBEDDING_MAX_SEQ_LENGTH", 512),
            embedding_batch_size=_env_int("EMBEDDING_BATCH_SIZE", 16),
            embedding_timeout_seconds=float(_env_str("EMBEDDING_TIMEOUT_SECONDS", "30")),
            job_lease_seconds=_env_int("JOB_LEASE_SECONDS", 120),
            job_max_attempts=_env_int("JOB_MAX_ATTEMPTS", 3),
            job_retry_base_seconds=_env_int("JOB_RETRY_BASE_SECONDS", 5),
            job_retry_max_seconds=_env_int("JOB_RETRY_MAX_SECONDS", 300),
            auto_consolidation=_env_str("AUTO_CONSOLIDATION", "0").lower() in {"1", "true", "yes", "on"},
            consolidation_interval_seconds=float(_env_str("CONSOLIDATION_INTERVAL_SECONDS", "1")),
            consolidation_batch_size=_env_int("CONSOLIDATION_BATCH_SIZE", 4),
        )

    def with_db(self, db_path: str | Path) -> "Settings":
        """换一个数据库路径，其余配置保持不变。"""
        return replace(self, db_path=Path(db_path))
