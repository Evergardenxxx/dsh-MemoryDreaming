"""MemoryDreaming —— 面向长期陪伴角色的持久记忆引擎。

最小的使用示例：

    from memorydreaming import MemoryRuntime, Scope

    runtime = MemoryRuntime(db_path=".memorydreaming/memory.db")
    scope = Scope(user_id="u_001", character_id="char_xiaoman")

    # 1) 恢复身份与未完活动
    bootstrap = runtime.bootstrap("u_001", "char_xiaoman")

    # 2) 保存用户这一轮说的话（会自动排入抽取任务）
    runtime.observe(turn, scope, idempotency_key="conv1#turn7")

    # 3) 提问前先取证据
    result = runtime.retrieve("上次答应的睡前故事", scope)

阅读顺序建议见 README.md 的「代码导读」。
"""

from .config import DeepSeekSchedulingPolicy, Settings
from .models import (
    Activity,
    ActivityStatus,
    BootstrapResult,
    CoreKind,
    CoreSnapshot,
    Correction,
    CorrectionResult,
    EpistemicStatus,
    EventRecord,
    ForgetResult,
    ForgetSelector,
    RelearningResult,
    MemoryDraft,
    MemoryItem,
    MemoryStatus,
    MemoryType,
    Modality,
    ObserveResult,
    SessionHandoff,
    Query,
    QueryPlan,
    RetrievalResult,
    Scope,
    Summary,
    SummaryHit,
    Turn,
)
from .retrieval.embedding import Embedder, HashingEmbedder
from .runtime import MemoryRuntime
from .host import HostAdapter, MemorySession, PreparedTurn, SessionBinding
from .llm_extractor import (
    EXTRACTION_SCHEMA,
    DeepSeekClient,
    DeepSeekExtractor,
    LLMExtractionError,
    LLMExtractor,
)
from .formation import MemoryFormationPolicy
from .llm import AdapterError, EstimatedTokenCounter, TokenCounter, tool_catalog
from .proposals import CoreProposalPolicy
from .scheduling import DeepSeekRequestScheduler
from .portability import export_snapshot, import_snapshot, load_snapshot, validate_snapshot, write_snapshot

__version__ = "0.23.0"

__all__ = [
    "MemoryRuntime",
    "DeepSeekSchedulingPolicy",
    "DeepSeekRequestScheduler",
    "HostAdapter",
    "MemorySession",
    "PreparedTurn",
    "SessionBinding",
    "CoreProposalPolicy",
    "AdapterError",
    "TokenCounter",
    "EstimatedTokenCounter",
    "tool_catalog",
    "Embedder",
    "HashingEmbedder",
    "Settings",
    "Scope",
    "Turn",
    "MemoryType",
    "Modality",
    "EpistemicStatus",
    "MemoryStatus",
    "MemoryItem",
    "MemoryDraft",
    "Activity",
    "ActivityStatus",
    "CoreKind",
    "CoreSnapshot",
    "EventRecord",
    "Query",
    "QueryPlan",
    "RetrievalResult",
    "Summary",
    "SummaryHit",
    "BootstrapResult",
    "ObserveResult",
    "SessionHandoff",
    "Correction",
    "CorrectionResult",
    "ForgetSelector",
    "ForgetResult",
    "RelearningResult",
    "LLMExtractor",
    "DeepSeekClient",
    "DeepSeekExtractor",
    "LLMExtractionError",
    "EXTRACTION_SCHEMA",
    "MemoryFormationPolicy",
    "export_snapshot",
    "import_snapshot",
    "write_snapshot",
    "load_snapshot",
    "validate_snapshot",
]
