"""运行配置。

所有可调参数都集中在这里，并且可以从环境变量覆盖。
新手最容易踩的坑是「魔法数字散落在各处」——所以像 48 小时窗口、候选预算、
token 预算这种架构文档里点名的参数，一律先在这里声明，再由各模块读取。
"""

from __future__ import annotations

import os
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

    # 单次关键词检索最多取多少条候选
    keyword_candidate_limit: int = 40

    # 向量通道：候选条数与相似度下限。
    # 下限不能省：任何两句中文都会共享一些二字词，没有阈值就全是噪声。
    # 也不能定太高——短查询和长记忆的余弦天然偏低。
    # 0.10 是实测出来的分界线：1024 维哈希嵌入下，无关问句稳定是 0.0，
    # 有明显字面重合的在 0.10~0.45。换嵌入模型后必须重新标定。
    vector_candidate_limit: int = 20
    vector_min_score: float = 0.10

    # bootstrap 时附带多少条「重要经历」摘要
    bootstrap_highlight_limit: int = 5

    @classmethod
    def from_env(cls) -> "Settings":
        """从环境变量构造配置，缺省项用上面的默认值。"""
        return cls(
            db_path=Path(_env_str("DB", str(DEFAULT_DB_PATH))),
            timezone=_env_str("TIMEZONE", "Asia/Shanghai"),
            recent_detail_window_hours=_env_int("RECENT_WINDOW_HOURS", 48),
            max_context_items=_env_int("MAX_ITEMS", 12),
            context_token_budget=_env_int("TOKEN_BUDGET", 1800),
        )

    def with_db(self, db_path: str | Path) -> "Settings":
        """换一个数据库路径，其余配置保持不变。"""
        return replace(self, db_path=Path(db_path))
