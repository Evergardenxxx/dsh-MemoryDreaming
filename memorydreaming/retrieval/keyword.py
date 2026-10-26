"""关键词检索：中文无第三方分词器时的可用基线。

为什么这里要单独处理「中文分词」？
SQLite 自带 FTS5 的默认分词器（unicode61）按空格和标点切词，
一整句中文会被当成一个巨大的 token，查「海边」永远匹配不到
「我们昨天聊到了海边的约定」。架构文档第 7 节明确提醒过这一点。

本项目第一版的做法（不引入 jieba 之类的依赖）：

1. 首选 FTS5 的 `trigram` 分词器。它把文本切成连续三字窗口，
   `MATCH "海边的"` 就能做子串匹配，中文效果不错，还自带 bm25 相关度排序。
   限制：查询词至少 3 个字。
2. 两字及以下的中文词（「橘猫」「海边」）走 `LIKE '%词%'` 子串匹配。
3. 如果运行环境的 SQLite 没有 FTS5/trigram，整体降级为纯 LIKE 扫描，
   并把降级结果记在 schema_meta 里，方便排查「为什么检索慢」。

这就是一个典型工程取舍：先保证正确可用，再在需要时换成真正的分词器。
换的时候只需要改这个文件，检索主流程不用动。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from ..models import MemoryType

# 用来切分句子的标点与空白（不含中文汉字本身）
_SPLIT_RE = re.compile(r"[\s，。！？、；：,.!?;:\"'“”‘’（）()\[\]【】《》〈〉…—\-~·]+")

def _marks(count: int) -> str:
    """生成 `?,?,?` 占位符，供 IN (...) 使用。"""
    return ",".join("?" for _ in range(count))


# 纯英文/数字片段
_ASCII_RE = re.compile(r"^[A-Za-z0-9_]+$")

# 汉字片段
_CJK_RE = re.compile(r"^[\u4e00-\u9fff]+$")

# 高频虚词：出现在查询里只会稀释相关度，直接丢掉。
# 注意不要把「约定」「答应」这类有信息量的词加进来——那属于四类记忆的路由信号。
_STOPWORDS = {
    "的", "了", "是", "在", "和", "与", "就", "也", "都", "还", "又",
    "我", "你", "他", "她", "它", "我们", "你们", "他们",
    "这个", "那个", "这些", "那些", "什么", "怎么", "为什么",
    "然后", "还有", "就是", "可以", "但是", "因为", "所以",
    "一下", "一点", "帮我", "请问",
}

# 一次查询最多取多少个词去做 FTS 匹配，防止长句把 SQL 撑爆
_MAX_TERMS = 10


@dataclass
class KeywordHit:
    """一条关键词命中。score 越大越相关。"""

    memory_id: str
    memory_type: MemoryType
    score: float
    matched_terms: list[str] = field(default_factory=list)


def _ngrams(run: str) -> list[str]:
    """把一段连续汉字切成二字词和三字词。

    为什么先出二字词？
    中文里双字词最常见（海边、约定、故事），召回最稳；
    三字词更精确但容易因为一字之差落空。两者都出，由「命中词数」来排序，
    相当于把「宁可多召回、再靠排序压噪」这条路走到底。

    例：「海边那次约定」→ 海边、边那、那次、次约、约定、海边那、边那次、那次约、次约定
    """
    if len(run) < 2:
        return []
    grams: list[str] = []
    for size in (2, 3):
        grams.extend(run[i : i + size] for i in range(0, len(run) - size + 1))
    return grams


def tokenize_query(text: str) -> list[str]:
    """把一句中文问题拆成若干个可检索的词。

    策略（简单但够用）：
      - 先按标点和空白切段，英文/数字段整段保留；
      - 汉字段切成二字词 + 三字词（见 _ngrams），去重后最多 10 个。

    这个做法不需要词典，代价是会切出很多无意义片段（「边那」）。
    但它们匹配不到东西，只会被排序规则自然淘汰；
    等以后接入 jieba 之类的分词器，只需要替换这个函数，调用方无感。
    """
    terms: list[str] = []
    seen: set[str] = set()

    def push(piece: str) -> None:
        if not piece or piece in _STOPWORDS or piece in seen:
            return
        seen.add(piece)
        terms.append(piece)

    for chunk in _SPLIT_RE.split(text):
        chunk = chunk.strip()
        if not chunk:
            continue

        # 中英混排时按字符类别再切一次
        for run in re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9_]+", chunk):
            if _ASCII_RE.match(run):
                if len(run) >= 2:
                    push(run.lower())
            elif _CJK_RE.match(run):
                for gram in _ngrams(run):
                    push(gram)
            else:
                # 单个汉字或单字符：信息量太低，跳过
                continue

    return terms[:_MAX_TERMS]


def text_shingles(text: str, sizes: tuple[int, ...] = (2, 3)) -> set[str]:
    """把文本切成字符 n-gram 的集合，用于相似度计算。

    和 tokenize_query 的区别：
    - tokenize_query 是「给检索用的词」，会丢停用词、有数量上限；
    - text_shingles 是「给相似度用的完整特征」，不丢词、不去重排序。

    去重（consolidation）和向量化（embedding）都用它，
    这样「两条记忆有多像」和「一条记忆怎么变成向量」用的是同一套切分方式，
    不会出现「去重觉得像、向量觉得不像」的割裂。
    """
    shingles: set[str] = set()
    for run in re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9_]+", text):
        run = run.lower()
        for size in sizes:
            if len(run) < size:
                continue
            shingles.update(run[i : i + size] for i in range(len(run) - size + 1))
    return shingles


def extract_entities(text: str) -> list[str]:
    """从问题里挑出「实体」候选：人名、宠物名、地点。

    第一版是启发式规则，不做真正的实体链接，
    目的是给检索加一个「必须包含这个名字」的过滤维度，减少误召回。
    """
    entities: list[str] = []

    # 引号里的内容基本一定是专名
    entities.extend(re.findall(r"[「『“\"]([^」』”\"]{1,12})[」』”\"]", text))

    # 「叫 X」「名字是 X」「名叫 X」后面的内容
    entities.extend(re.findall(r"(?:叫做|叫|名字是|名叫)\s*([\u4e00-\u9fffA-Za-z]{1,12})", text))

    # 英文专名（大写开头）
    entities.extend(re.findall(r"\b([A-Z][a-z]{2,})\b", text))

    unique: list[str] = []
    for item in entities:
        item = item.strip()
        if item and item not in unique:
            unique.append(item)
    return unique


class KeywordIndex:
    """维护 memories 的全文索引，并提供子串检索。

    索引是「派生数据」：任何时候都可以从 memories 表重建。
    这一点很重要——换分词器、恢复备份、批量删除后重建索引，
    都不应该需要改业务代码。
    """

    META_KEY = "keyword_tokenizer"
    TABLE = "memories_fts"

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # -- 索引维护 ---------------------------------------------------------

    @property
    def tokenizer(self) -> str:
        """当前生效的分词器：'trigram' 或 'none'（纯 LIKE 降级）。"""
        return self._meta_get(self.META_KEY) or "none"

    def ensure_index(self) -> str:
        """确保索引表与触发器存在，返回生效的分词器名字。

        会在下面三种情况重建索引：
          - 第一次初始化；
          - 上次运行没有 FTS 支持，这次有了；
          - schema_meta 里记录的分词器与期望不一致。
        """
        current = self._meta_get(self.META_KEY)
        if current == "trigram" and self._table_exists():
            self._ensure_triggers()
            return "trigram"

        # 清掉旧索引（可能用了不同的分词器），避免新旧混用
        self._conn.execute(f"DROP TABLE IF EXISTS {self.TABLE}")
        self._drop_triggers()

        if self._create_trigram_table():
            self._ensure_triggers()
            self._meta_set(self.META_KEY, "trigram")
            self.rebuild()
            return "trigram"

        # 没有 FTS5 就把选择记下来，检索那侧会自动走 LIKE
        self._meta_set(self.META_KEY, "none")
        return "none"

    def rebuild(self) -> None:
        """从 memories 表全量重建索引。"""
        if self.tokenizer != "trigram":
            return
        self._conn.execute(f"DELETE FROM {self.TABLE}")
        self._conn.execute(
            f"""
            INSERT INTO {self.TABLE} (memory_id, body_text)
            SELECT memory_id, body_text FROM memories WHERE status != 'deleted'
            """
        )
        self._conn.commit()

    def _create_trigram_table(self) -> bool:
        try:
            self._conn.execute(
                f"""
                CREATE VIRTUAL TABLE {self.TABLE} USING fts5(
                    memory_id UNINDEXED,
                    body_text,
                    tokenize='trigram'
                )
                """
            )
            return True
        except sqlite3.OperationalError:
            return False

    def _ensure_triggers(self) -> None:
        """用触发器保持索引与主表同步。

        也可以选择在 repository 里手动同步，但触发器更不容易漏；
        代价是「索引更新」这件事变得隐式，所以这里写清楚注释。
        """
        self._conn.executescript(
            f"""
            CREATE TRIGGER IF NOT EXISTS memories_fts_ai AFTER INSERT ON memories BEGIN
                INSERT INTO {self.TABLE} (memory_id, body_text)
                VALUES (new.memory_id, new.body_text);
            END;

            CREATE TRIGGER IF NOT EXISTS memories_fts_ad AFTER DELETE ON memories BEGIN
                DELETE FROM {self.TABLE} WHERE memory_id = old.memory_id;
            END;

            -- 只有正文变化时才重建这一行；改状态、改访问次数不触发
            CREATE TRIGGER IF NOT EXISTS memories_fts_au AFTER UPDATE OF body_text ON memories BEGIN
                DELETE FROM {self.TABLE} WHERE memory_id = old.memory_id;
                INSERT INTO {self.TABLE} (memory_id, body_text)
                VALUES (new.memory_id, new.body_text);
            END;
            """
        )

    def _drop_triggers(self) -> None:
        for name in ("memories_fts_ai", "memories_fts_ad", "memories_fts_au"):
            self._conn.execute(f"DROP TRIGGER IF EXISTS {name}")

    def _table_exists(self) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (self.TABLE,)
        ).fetchone()
        return row is not None

    def _meta_get(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM schema_meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def _meta_set(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    # -- 检索 -------------------------------------------------------------

    def search(
        self,
        terms: list[str],
        scope_user_id: str,
        character_id: str,
        memory_types: list[MemoryType] | None = None,
        limit: int = 40,
        include_statuses: tuple[str, ...] = ("active", "superseded"),
    ) -> list[KeywordHit]:
        """按词检索，返回按「命中词数 + 相关度」排序的候选。

        参数:
            terms: 已分好的词，通常来自 tokenize_query。
            scope_user_id / character_id: 可见性过滤，必须在检索前生效。
            memory_types: 只查这几类；None 表示不限。
            limit: 返回上限。
        """
        if not terms:
            return []

        fts_terms = [t for t in terms if len(t) >= 3][:_MAX_TERMS]
        like_terms = [t for t in terms if 1 <= len(t) < 3][:_MAX_TERMS]

        best_score: dict[str, float] = {}
        hit_count: dict[str, int] = {}
        type_of: dict[str, MemoryType] = {}
        matched: dict[str, list[str]] = {}

        for term in fts_terms:
            for memory_id, memory_type, score in self._search_fts(
                term, scope_user_id, character_id, memory_types, limit, include_statuses
            ):
                type_of[memory_id] = memory_type
                hit_count[memory_id] = hit_count.get(memory_id, 0) + 1
                best_score[memory_id] = max(best_score.get(memory_id, 0.0), score)
                matched.setdefault(memory_id, []).append(term)

        for term in like_terms:
            for memory_id, memory_type in self._search_like(
                term, scope_user_id, character_id, memory_types, limit, include_statuses
            ):
                type_of[memory_id] = memory_type
                hit_count[memory_id] = hit_count.get(memory_id, 0) + 1
                # LIKE 没有相关度，给一个固定中等分，让「命中词数」主导排序
                best_score[memory_id] = max(best_score.get(memory_id, 0.5), 0.5)
                matched.setdefault(memory_id, []).append(term)

        # 排序规则：命中词数多的优先，其次相关度高的优先
        ordered = sorted(
            best_score,
            key=lambda mid: (-hit_count[mid], -best_score[mid], mid),
        )
        return [
            KeywordHit(
                memory_id=mid,
                memory_type=type_of[mid],
                score=best_score[mid],
                matched_terms=matched.get(mid, []),
            )
            for mid in ordered[:limit]
        ]

    def _search_fts(
        self,
        term: str,
        user_id: str,
        character_id: str,
        memory_types: list[MemoryType] | None,
        limit: int,
        include_statuses: tuple[str, ...],
    ) -> list[tuple[str, MemoryType, float]]:
        if self.tokenizer != "trigram":
            return []

        # MATCH 里的双引号是 FTS5 的字符串定界符，词里出现要转义
        escaped = term.replace('"', '""')
        sql = f"""
            SELECT m.memory_id, m.memory_type, bm25({self.TABLE}) AS bm
            FROM {self.TABLE}
            JOIN memories m ON m.memory_id = {self.TABLE}.memory_id
            WHERE {self.TABLE} MATCH ?
              AND m.user_id = ?
              AND m.character_id = ?
              AND m.status IN ({_marks(len(include_statuses))})
        """
        params: list[object] = [f'"{escaped}"', user_id, character_id, *include_statuses]

        if memory_types:
            sql += f" AND m.memory_type IN ({_marks(len(memory_types))})"
            params.extend(t.value for t in memory_types)

        sql += " ORDER BY bm LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(sql, params).fetchall()
        # bm25 越小越相关，且是负数；取负号变成「越大越相关」
        return [(row[0], MemoryType(row[1]), -float(row[2])) for row in rows]

    def _search_like(
        self,
        term: str,
        user_id: str,
        character_id: str,
        memory_types: list[MemoryType] | None,
        limit: int,
        include_statuses: tuple[str, ...],
    ) -> list[tuple[str, MemoryType]]:
        sql = """
            SELECT m.memory_id, m.memory_type
            FROM memories m
            WHERE m.body_text LIKE ?
              AND m.user_id = ?
              AND m.character_id = ?
              AND m.status IN ({statuses})
        """.format(statuses=",".join("?" * len(include_statuses)))
        params: list[object] = [f"%{term}%", user_id, character_id, *include_statuses]

        if memory_types:
            sql += f" AND m.memory_type IN ({_marks(len(memory_types))})"
            params.extend(t.value for t in memory_types)

        sql += " LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(sql, params).fetchall()
        return [(row[0], MemoryType(row[1])) for row in rows]
