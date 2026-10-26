"""领域模型：把架构文档里的概念，落成可以直接读懂的 Python 数据结构。

这里是整个项目的「共同语言」：存储层、检索层、运行时都只认这些结构。
建议的阅读顺序：

    models.py（有哪些东西）
      → storage/schema.sql（它们怎么落到数据库）
      → storage/repository.py（怎么读写）
      → runtime.py（对外怎么用）
      → retrieval/pipeline.py（怎么把它们找回来）

三个设计取舍，先说明，免得读代码时困惑：

1. 不用 ORM。表结构写在 schema.sql 里，SQL 写在 repository 里。
   多一层 ORM 会让「数据到底怎么存的」变得不直观，而本项目正想讲清楚这件事。

2. 四类记忆共用一张 `memories` 表，区别体现在 `memory_type` 和 `body` 上。
   架构文档允许「同一数据库中的结构化表或类型记录」，共用一张表的好处是
   检索、纠错、删除的传播逻辑只写一遍。

3. 时间一律用带时区的 UTC datetime 表示，出库入库时转 ISO8601 字符串。
   数据库里字符串的字典序就等于时间先后，SQL 排序不会出错。
"""

from __future__ import annotations

import uuid
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from .clock import ensure_utc, utcnow


def new_id(prefix: str) -> str:
    """生成带前缀的短 ID，例如 `mem_3f9a1c0b7d2e`。

    用前缀而不是纯 UUID 的原因很实际：调试时看一眼日志就知道这是哪类对象。
    """
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# 词汇表：这些字符串同时会写进数据库，改动时必须同步改 schema.sql 的 CHECK 约束
# ---------------------------------------------------------------------------


class MemoryType(str, Enum):
    """四类记忆。继承 str 是为了能直接和数据库里的字符串比较、做 JSON 键。"""

    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"

    @property
    def label_zh(self) -> str:
        """中文标签，用于 CLI 输出和上下文渲染。"""
        return {
            "working": "工作",
            "episodic": "情景",
            "semantic": "语义",
            "procedural": "程序",
        }[self.value]


class Modality(str, Enum):
    """模态：这条内容在现实里是什么性质。

    架构文档第 3 节特别强调：模型说「我们一起去过海边」不能证明用户真的去过。
    所以「实际发生」和「共同虚构」「引用」「假设」必须分开记。
    """

    OCCURRED = "occurred"            # 实际发生的对话或事件
    INTENTION = "intention"          # 意愿、计划（「我想养猫」）
    HYPOTHETICAL = "hypothetical"    # 假设、推演
    QUOTED = "quoted"                # 引用别处的话
    FICTIONAL = "fictional"          # 共同创作/角色扮演世界里的设定


class EpistemicStatus(str, Enum):
    """认识论状态：这条记忆的依据有多硬。

    importance 表达「值得记住」，retrieval_strength 表达「容易想起」，
    这个字段表达「凭什么相信它」——三者不能互相替代。
    """

    EXPLICIT_SETTING = "explicit_setting"        # 用户明确设定（名字、称呼、雷区）
    USER_REPORT = "user_report"                  # 用户自述（「我已经养了一只猫」）
    OBSERVED_INTERACTION = "observed_interaction"  # 系统实际观察到的对话行为
    INFERRED = "inferred"                        # 模型归纳或推测，需要证据支撑


class MemoryStatus(str, Enum):
    """记忆的生死状态。

    - active：当前有效
    - superseded：被更新的事实取代（旧值仍可查，用于解释历史）
    - retracted：被判定为「原先就是错的」，不再作为依据
    - deleted：用户要求删除，已从检索索引移除
    """

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"
    DELETED = "deleted"


class ActivityStatus(str, Enum):
    """活动状态。活动是「一段可以跨窗口继续的事」，比如持续几天的故事。"""

    ACTIVE = "active"
    PAUSED = "paused"
    CLOSED = "closed"


class CoreKind(str, Enum):
    """核心档案的两种投影：我是谁（身份）／我们是什么关系（关系）。"""

    IDENTITY = "identity"
    RELATIONSHIP = "relationship"


class JobStatus(str, Enum):
    """异步整理任务的状态机。"""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class LinkRelation(str, Enum):
    """记忆之间的链接类型（架构文档第 3 节末尾）。"""

    SUPPORTS = "supports"          # A 为 B 提供证据
    DERIVED_FROM = "derived_from"  # A 由 B 派生（B 变了 A 可能失效）
    SUPERSEDES = "supersedes"      # A 取代 B
    PART_OF = "part_of"            # A 是 B 的一部分


# ---------------------------------------------------------------------------
# 作用域与输入
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Scope:
    """可见范围。检索前先按它过滤，这是「不同角色、不同关系不串味」的基础。

    架构文档第 2 节：角色身份通过 user_id + character_id 直接定位；
    新窗口的 conversation_id 只作为来源记录，不作为长期记忆的分区主键。
    """

    user_id: str
    character_id: str
    relationship_id: str | None = None
    activity_id: str | None = None
    domain: str = "real"  # real=真实世界；shared_fiction=共同创作世界（虚构属性单独记）

    def __post_init__(self) -> None:
        # 这两个字段是主键的一部分，空字符串会让不同角色的数据混在一起
        if not self.user_id or not self.character_id:
            raise ValueError("Scope 必须包含 user_id 和 character_id")


@dataclass
class Turn:
    """一轮对话中的一条消息，`observe()` 的输入。

    speaker 用固定字符串：user（用户）/ character（角色）/ system（系统）。
    occurred_at 是「这件事发生的时间」，不是「写入数据库的时间」——
    两者在补录历史对话时会不一样，架构文档要求分开存。
    """

    speaker: str
    content: str
    occurred_at: datetime
    conversation_id: str
    source_timezone: str = "Asia/Shanghai"
    activity_id: str | None = None
    occurred_precision: str = "exact"  # exact=精确到秒 / day=只到日 / unknown=说不清


# ---------------------------------------------------------------------------
# 四类记忆的正文结构
# ---------------------------------------------------------------------------


@dataclass
class WorkingBody:
    """工作记忆正文：当前交流的目标与未完成事项。

    工作记忆跟着 activity_id 走。同一个角色可以同时有几段活动（故事、学习、日常），
    互不干扰——这是架构文档里「同角色的不同活动不混淆」那条验收场景。
    """

    goal: str = ""                                  # 这段活动想达成什么
    open_loops: list[str] = field(default_factory=list)   # 未完成事项、已答应的事
    active_entities: list[str] = field(default_factory=list)  # 当前在场的人/物
    checkpoint_note: str = ""                       # 一句话进度备注


@dataclass
class EpisodicBody:
    """情景记忆正文：一次可识别的事件。"""

    what_happened: str = ""                             # 发生了什么
    participants: list[str] = field(default_factory=list)  # 参与者
    context: str = ""                                   # 当时的场景/氛围
    outcome: str = ""                                   # 结果


@dataclass
class SemanticBody:
    """语义记忆正文：一条事实或偏好，用三元组表达方便校验。"""

    subject: str = ""     # 主体，通常是 user 或角色名
    predicate: str = ""   # 关系，如 名字是 / 喜欢 / 住在
    obj: str = ""         # 客体
    scope_note: str = ""  # 适用范围说明（如「仅限共同虚构世界」）


@dataclass
class ProceduralBody:
    """程序记忆正文：一套被验证过的相处规则或解决步骤。"""

    trigger: str = ""                                     # 什么情况下触发
    preconditions: list[str] = field(default_factory=list)  # 需要满足的前置条件
    steps: list[str] = field(default_factory=list)          # 步骤
    exceptions: list[str] = field(default_factory=list)     # 例外情况
    expected_outcome: str = ""                              # 预期结果
    supporting_examples: list[str] = field(default_factory=list)  # 支持它的实际例子


def body_to_dict(body: Any) -> dict[str, Any]:
    """把正文 dataclass 转成可 JSON 序列化的 dict。

    已经就是 dict 的输入原样返回，这样调用方既能用 dataclass（有类型提示），
    也能直接传 dict（写测试时更省事）。
    """
    if isinstance(body, dict):
        return dict(body)
    # dataclasses.asdict 会递归处理嵌套 dataclass，但我们这里没有嵌套
    from dataclasses import asdict

    return asdict(body)


def body_to_text(memory_type: MemoryType, body: dict[str, Any]) -> str:
    """把正文拼成一段用于关键词检索的纯文本。

    刻意「只要值、不要键」：键名是英文（goal、predicate…），
    混进检索文本会干扰中文匹配。

    memory_type 参数目前没有分支，保留它是为了将来给不同类型加
    不同的拼接策略（例如程序记忆可以把 steps 拆成独立可检索条目）。
    """
    parts: list[str] = []
    for value in body.values():
        if isinstance(value, str):
            if value.strip():
                parts.append(value.strip())
        elif isinstance(value, (list, tuple)):
            parts.extend(str(item).strip() for item in value if str(item).strip())
        elif isinstance(value, dict):
            # 嵌套结构少见，简单展开一层即可
            parts.extend(str(v).strip() for v in value.values() if str(v).strip())
        elif value is not None:
            parts.append(str(value))
    return "；".join(parts)


# ---------------------------------------------------------------------------
# 核心对象
# ---------------------------------------------------------------------------


@dataclass
class MemoryItem:
    """一条记忆。四类记忆共用这个外壳，差异都在 body 里。

    字段分四组来读会更清楚：
      1) 身份：memory_id / memory_type / schema_version / scope
      2) 来源：source_event_ids / source_speaker / dedupe 相关字段
      3) 效力：epistemic_status / modality / 时间范围 / status
      4) 权重：importance / retrieval_strength / 访问统计
    """

    memory_id: str
    memory_type: MemoryType
    scope: Scope
    body: dict[str, Any]

    # --- 来源 ---
    source_event_ids: list[str] = field(default_factory=list)
    source_speaker: str | None = None

    # --- 效力 ---
    epistemic_status: EpistemicStatus = EpistemicStatus.USER_REPORT
    modality: Modality = Modality.OCCURRED
    occurred_start: datetime | None = None
    occurred_end: datetime | None = None
    occurred_precision: str = "unknown"
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    supersedes_id: str | None = None
    status: MemoryStatus = MemoryStatus.ACTIVE
    schema_version: int = 1

    # --- 权重 ---
    importance: float = 0.5            # 值得记住的程度
    retrieval_strength: float = 0.5    # 容易想起的程度（可随使用衰减/增强）
    last_used_at: datetime | None = None
    access_count: int = 0

    # --- 其他 ---
    recorded_at: datetime = field(default_factory=utcnow)
    body_text: str = ""                # 检索投影，入库时一并保存
    source_spans: dict[str, list[list[int]]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # 调用方可以显式给 body_text（例如从数据库读出时），否则自动生成
        for event_id, intervals in self.source_spans.items():
            if event_id not in self.source_event_ids or not isinstance(intervals, list):
                raise ValueError("来源片段必须属于已声明的事件")
            for interval in intervals:
                if (not isinstance(interval, (list, tuple)) or len(interval) != 2
                        or any(isinstance(value, bool) or not isinstance(value, int) for value in interval)
                        or not 0 <= interval[0] < interval[1]):
                    raise ValueError("来源片段必须为有效的 Unicode 码点区间")
        if not self.body_text:
            self.body_text = body_to_text(self.memory_type, self.body)

    def is_current(self, now: datetime) -> bool:
        """这条记忆在 `now` 时刻是否仍然成立。

        注意「成立」和「可检索」是两件事：过期的偏好仍然可以被检索到，
        只是要标注成历史值，避免模型拿旧称呼回答今天的问题。
        """
        if self.status is not MemoryStatus.ACTIVE:
            return False
        if self.valid_from is not None and ensure_utc(self.valid_from) > ensure_utc(now):
            return False
        if self.valid_to is None:
            return True
        return ensure_utc(self.valid_to) > ensure_utc(now)

    def render(self, limit: int = 200) -> str:
        """按类型把正文排成一句人话，用于上下文与命令行展示。

        为什么不直接用 body_text？
        那是检索用的扁平文本（用「；」把每个字段串起来），适合匹配、不适合阅读。
        展示时按类型组织，模型更容易抓住「谁 对谁 做了什么」。
        """
        body = self.body
        text = ""

        if self.memory_type is MemoryType.SEMANTIC:
            parts = [
                str(body.get("subject", "")),
                str(body.get("predicate", "")),
                str(body.get("obj", "")),
            ]
            text = " ".join(part for part in parts if part)

        elif self.memory_type is MemoryType.EPISODIC:
            text = str(body.get("what_happened", ""))
            participants = body.get("participants") or []
            if participants:
                text += f"（参与者：{'、'.join(str(p) for p in participants)}）"

        elif self.memory_type is MemoryType.WORKING:
            pieces: list[str] = []
            if body.get("goal"):
                pieces.append(f"目标：{body['goal']}")
            loops = body.get("open_loops") or []
            if loops:
                pieces.append("未完成：" + "；".join(str(loop) for loop in loops))
            text = "；".join(pieces)

        else:  # PROCEDURAL
            pieces = []
            if body.get("trigger"):
                pieces.append(f"触发：{body['trigger']}")
            steps = body.get("steps") or []
            if steps:
                pieces.append("步骤：" + " → ".join(str(step) for step in steps))
            text = "；".join(pieces)

        text = " ".join(text.split()) or self.body_text
        return text if len(text) <= limit else text[:limit] + "…"

    def summary(self, limit: int = 80) -> str:
        """压成一行，方便 CLI 与日志展示。"""
        return self.render(limit)


@dataclass
class Activity:
    """一段可以跨窗口继续的活动，同时也是它的持久工作检查点。"""

    activity_id: str
    scope: Scope
    title: str
    status: ActivityStatus = ActivityStatus.ACTIVE
    goal: str = ""
    open_loops: list[str] = field(default_factory=list)
    active_entities: list[str] = field(default_factory=list)
    checkpoint: dict[str, Any] = field(default_factory=dict)
    # 每次 checkpoint 成功写入都会 +1；更新时带上期望版本，防止并发覆盖
    checkpoint_version: int = 1
    last_event_at: datetime | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class SessionHandoff:
    """上下文压缩或窗口结束时留下的短交接记录。

    handoff 是活动检查点的一个宿主侧投影：它保存下一窗口需要的最小
    工作集合，不复制完整对话，也不构成新的事实来源。历史交接保留在库中
    便于审计，读取时只返回同一作用域下最新的 active 记录。
    """

    handoff_id: str
    scope: Scope
    conversation_id: str
    activity_id: str | None
    payload: dict[str, Any]
    idempotency_key: str
    source_event_id: str | None = None
    activity_version: int | None = None
    status: str = "active"  # active=最新交接；superseded=被后续交接取代
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class CoreSnapshot:
    """身份核心或关系档案的一个版本快照。

    「版本化投影」的意思是：核心不是一段随便改的文本，
    而是每次变更都留一条新记录，带证据、原因、生效时间，可以解释「为什么变成现在这样」。
    """

    scope: Scope
    kind: CoreKind
    core_version: int
    payload: dict[str, Any]
    change_reason: str
    effective_from: datetime
    created_at: datetime = field(default_factory=utcnow)
    evidence_ids: list[str] = field(default_factory=list)
    status: str = "active"

    # 每个字段单独引用来源；旧库无法细分时沿用该版本的粗粒度来源。
    field_evidence_ids: dict[str, list[str]] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        """读核心字段的快捷方法，例如 core.get("display_name")。"""
        return self.payload.get(key, default)


@dataclass
class Summary:
    """主题摘要：一段时期内若干条记忆的「索引卡」。

    它刻意不是第五类记忆，也不存全文：
    摘要是**派生索引**，只保留主题、时间范围和一句概述，
    真正的事实仍然在它引用的记忆里。这样做的原因很实际——
    摘要在反复压缩中会失真，如果它成了唯一底座，错误就再也回不去了。
    """

    summary_id: str
    scope: Scope
    topic: str
    digest: str
    source_memory_ids: list[str] = field(default_factory=list)
    source_event_ids: list[str] = field(default_factory=list)
    source_digest: str = ""
    algorithm_version: str = "extractive-v1"
    span_start: datetime | None = None
    span_end: datetime | None = None
    importance: float = 0.5
    status: str = "active"  # active=可用；stale=来源已变化，等下一次重建
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


def memory_source_digest(items: list[MemoryItem], algorithm_version: str) -> str:
    """为派生摘要计算稳定来源指纹，不复制原文到摘要表。"""
    material = [
        {
            "memory_id": item.memory_id,
            "memory_type": item.memory_type.value,
            "body": item.body,
            "source_event_ids": sorted(item.source_event_ids),
            "status": item.status.value,
            "valid_from": item.valid_from.isoformat() if item.valid_from else None,
            "valid_to": item.valid_to.isoformat() if item.valid_to else None,
        }
        for item in sorted(items, key=lambda value: value.memory_id)
    ]
    payload = json.dumps([algorithm_version, material], ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class SummaryHit:
    """一次摘要检索的命中，score 与记忆检索同口径（越大越相关）。"""

    summary: Summary
    score: float


@dataclass
class EventRecord:
    """原始事件：用户实际说过的话。append-only，是全部记忆的证据底座。

    字段不多，但每个都有明确职责：
    occurred_at（事件时间）与 recorded_at（记录时间）分开，
    才能正确处理「今天说十年前搬过家」这种情况。
    """

    event_id: str
    scope: Scope
    conversation_id: str
    speaker: str
    content: str
    occurred_at: datetime
    source_timezone: str = "Asia/Shanghai"
    occurred_precision: str = "exact"
    recorded_at: datetime = field(default_factory=utcnow)
    turn_index: int | None = None
    idempotency_key: str | None = None


# ---------------------------------------------------------------------------
# 检索相关的输入与输出
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TimeHint:
    """从问题里解析出来的时间意图。

    hard=True 表示这是硬约束（用户给了明确日期），不能因为「48 小时外」就模糊化；
    hard=False 表示只是大致范围，可以扩大范围再找。
    """

    label: str                    # 命中的原文，如「昨天」
    start: datetime | None
    end: datetime | None
    precision: str                # exact / day / week / month / year / coarse
    hard: bool


@dataclass(frozen=True)
class ReadFilter:
    """宿主给定的额外读取限制；None 活动表示共享记忆，空元组表示全部拒绝。"""

    activity_ids: tuple[str | None, ...] | None = None
    exclude_event_ids: tuple[str, ...] = ()


@dataclass
class Query:
    """一次检索请求。`retrieve()` 的入参。

    多数参数可以省略，由 planner 依据问题文本推断；
    显式传入的优先级更高（例如调用方已经算好了时间范围）。
    """

    text: str
    scope: Scope
    time_range: tuple[datetime, datetime] | None = None
    precision: str | None = None
    max_items: int | None = None
    token_budget: int | None = None
    # 时间旅行：「按 as_of 那一刻的状态来回答」。用于复盘「当时我们是怎么以为的」
    as_of: datetime | None = None
    read_filter: ReadFilter | None = None
    detail: str = "auto"


@dataclass
class QueryPlan:
    """查询计划：检索前的「想清楚要什么」。

    架构文档第 4 节要求先形成计划再检索：
    四类相关分数（多标签，不要求和为 1）、时间范围、精度、候选预算。
    """

    query_text: str
    type_scores: dict[MemoryType, float]
    per_type_budget: dict[MemoryType, int]
    keywords: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    time_hint: TimeHint | None = None
    precision: str = "coarse"
    hard_time: bool = False
    # 这个问题是不是在「接着刚才的话说」？只有这种情况才启用最近窗口通道。
    # 否则「量子力学是什么」也会把今天的聊天记录塞进上下文，属于噪声。
    prefers_recent: bool = False
    expansion_mode: str = "summary_then_expand"  # detail / summary_then_expand / exact_lookup
    max_items: int = 12
    token_budget: int = 1800
    as_of: datetime | None = None
    # 低信息输入（问候、确认、告别）只保存事件，不触发记忆检索。
    skip_retrieval: bool = False
    generated_at: datetime = field(default_factory=utcnow)

    def top_types(self, limit: int = 2) -> list[MemoryType]:
        """相关度最高的几类，仅用于日志/调试展示。"""
        ordered = sorted(self.type_scores.items(), key=lambda kv: kv[1], reverse=True)
        return [memory_type for memory_type, _ in ordered[:limit]]


@dataclass
class RetrievedItem:
    """检索结果里的一条记忆，附带「为什么被选中」。"""

    memory: MemoryItem
    score: float
    channels: list[str] = field(default_factory=list)
    valid_now: bool = True
    note: str = ""

    @property
    def memory_id(self) -> str:
        return self.memory.memory_id


@dataclass
class RetrievalTrace:
    """检索轨迹：给调试和评估用，不是给模型看的正文。

    架构文档要求 `retrieve` 能返回检索轨迹，否则无法回答
    「为什么这条记忆没被找到」这类问题。
    """

    channel_hits: dict[str, int] = field(default_factory=dict)
    fused_candidates: int = 0
    selected: int = 0
    dropped: int = 0
    token_estimate: int = 0
    elapsed_ms: float = 0.0
    notes: list[str] = field(default_factory=list)


@dataclass
class RetrievalResult:
    """检索结果。items 是证据，summaries 是「先看哪几张索引卡」，gaps 是缺口。"""

    query_text: str
    plan: QueryPlan
    items: list[RetrievedItem] = field(default_factory=list)
    summaries: list[SummaryHit] = field(default_factory=list)
    trace: RetrievalTrace = field(default_factory=RetrievalTrace)
    gaps: list[str] = field(default_factory=list)

    def memory_ids(self) -> list[str]:
        return [item.memory_id for item in self.items]

    def summary_ids(self) -> list[str]:
        return [hit.summary.summary_id for hit in self.summaries]


@dataclass
class BootstrapResult:
    """启动恢复结果。宿主拿它组装第一轮上下文。

    identity / relationship 为 None 表示恢复失败，此时 recovery_warning 一定有值：
    宿主应当把「恢复失败」如实告诉用户，而不是用临时身份顶替原角色。
    """

    scope: Scope
    identity: CoreSnapshot | None = None
    relationship: CoreSnapshot | None = None
    activities: list[Activity] = field(default_factory=list)
    selected_activity: Activity | None = None
    highlights: list[MemoryItem] = field(default_factory=list)
    # 主题索引卡：让角色一开口就知道「我们之间有哪些话题」，而不是逐条翻记忆
    summaries: list[Summary] = field(default_factory=list)
    core_version: int | None = None
    recovery_warning: str | None = None


@dataclass
class ObserveResult:
    """一次 observe 的结果。duplicate=True 表示幂等键命中，本轮没有重复写入。"""

    event: EventRecord
    duplicate: bool = False
    job_id: str | None = None
    activity_version: int | None = None


@dataclass
class Correction:
    """纠错请求。

    kind 的两种语义必须分开（架构文档第 6 节）：
      - "fix"    ：原先就是错的 → 撤回旧记录，并让派生内容失效
      - "change" ：当时正确、现在改变 → 结束旧事实有效期，写一条新的
    """

    kind: str
    statement: str = ""
    corrected_body: dict[str, Any] | None = None
    evidence_event_ids: list[str] = field(default_factory=list)
    effective_from: datetime | None = None


@dataclass
class CorrectionResult:
    """纠错结果：旧记录变成了什么、新记录是哪条、连带失效了哪些派生记忆。"""

    old_memory: MemoryItem
    new_memory: MemoryItem | None = None
    invalidated_derived: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class ForgetSelector:
    """删除选择器。按 ID 删，或按范围+类型批量删。

    删除必须传播到原文索引、派生记录（架构文档第 6 节），
    所以这里保留 scope 维度，避免误删其他角色的数据。
    """

    reason: str = ""
    memory_ids: list[str] | None = None
    event_ids: list[str] | None = None
    scope: Scope | None = None
    memory_types: list[MemoryType] | None = None
    domain: str | None = None


@dataclass
class ForgetResult:
    """删除结果。deleted_ids 便于宿主向用户回执「删掉了哪些」。"""

    deleted_ids: list[str] = field(default_factory=list)
    invalidated_derived: list[str] = field(default_factory=list)
    note: str = ""
    tombstone_ids: list[str] = field(default_factory=list)
    core_fields_removed: list[str] = field(default_factory=list)
    conservative_event_ids: list[str] = field(default_factory=list)
    forgotten_event_ids: list[str] = field(default_factory=list)
    erasure_level: str = "logical_suppressed"


@dataclass
class MemoryDraft:
    """记忆候选：抽取器的输出，尚未入库。

    `candidate_key` 由抽取器自己保证同一事件内唯一，
    配合 source_event_id + extractor_version 形成幂等键，
    避免重跑抽取时写出重复记忆（架构文档第 6 节）。
    """

    memory_type: MemoryType
    body: dict[str, Any]
    candidate_key: str
    epistemic_status: EpistemicStatus = EpistemicStatus.OBSERVED_INTERACTION
    modality: Modality = Modality.OCCURRED
    occurred_start: datetime | None = None
    occurred_end: datetime | None = None
    occurred_precision: str = "exact"
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    importance: float = 0.5
    source_speaker: str | None = None
    source_span: tuple[int, int] | None = None
