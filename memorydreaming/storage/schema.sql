-- MemoryDreaming 数据库结构（SQLite）
-- ============================================================================
-- 设计说明：
-- 1. 时间统一存 UTC 的 ISO8601 字符串（如 2026-10-24T09:30:00.000000+00:00）。
--    这样字符串的字典序 = 时间先后，SQL 里可以直接比较和排序。
-- 2. 列表类字段（证据 ID、未完成事项等）以 JSON 文本存放。第一版不拆子表，
--    因为它们的读写总是跟着主记录一起，拆开只会增加连接成本。
-- 3. CHECK 约束把 Python 枚举值写进数据库，写错值会当场报错，而不是留下脏数据。
--    改动枚举时请同步修改这里的约束，并提升 schema 版本号。
-- 4. 所有表都带 user_id + character_id：不同角色、不同关系的记忆必须能隔离。
-- ============================================================================

-- 元信息：schema 版本、关键词索引所用的分词器等
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ----------------------------------------------------------------------------
-- 身份核心 / 关系档案：版本化投影
-- ----------------------------------------------------------------------------
-- 为什么用 (user_id, character_id, kind, core_version) 做主键？
-- 版本号必须在「同一个角色」内连续递增，才能做乐观并发控制。
-- 全局自增 ID 做不到这一点：别的角色写一次，你的版本号就跳了。
CREATE TABLE IF NOT EXISTS core_versions (
    user_id        TEXT    NOT NULL,
    character_id   TEXT    NOT NULL,
    kind           TEXT    NOT NULL CHECK (kind IN ('identity', 'relationship')),
    core_version   INTEGER NOT NULL,
    relationship_id TEXT,
    payload_json   TEXT    NOT NULL,
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    change_reason  TEXT    NOT NULL,
    -- active=当前生效；superseded=被新版本取代（仍可回退）；rejected=候选被否决
    status         TEXT    NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active', 'superseded', 'rejected')),
    effective_from TEXT    NOT NULL,
    created_at     TEXT    NOT NULL,
    PRIMARY KEY (user_id, character_id, kind, core_version)
);

CREATE INDEX IF NOT EXISTS idx_core_lookup
    ON core_versions (user_id, character_id, kind, core_version DESC);

-- ----------------------------------------------------------------------------
-- 活动：跨窗口继续的事情，同时是持久化的工作检查点
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS activities (
    activity_id     TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    character_id    TEXT NOT NULL,
    relationship_id TEXT,
    domain          TEXT NOT NULL DEFAULT 'real',
    title           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active', 'paused', 'closed')),
    goal            TEXT NOT NULL DEFAULT '',
    open_loops_json TEXT NOT NULL DEFAULT '[]',
    active_entities_json TEXT NOT NULL DEFAULT '[]',
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    -- 每次成功的 checkpoint 都会 +1，配合 expected_version 做条件更新
    checkpoint_version INTEGER NOT NULL DEFAULT 1,
    last_event_at   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_activities_scope
    ON activities (user_id, character_id, status, updated_at DESC);

-- ----------------------------------------------------------------------------
-- 原始事件：append-only 的证据底座
-- ----------------------------------------------------------------------------
-- 记忆可以重算、可以被推翻，但「用户当时到底说了什么」必须保留原文，
-- 否则纠正、删除、来源回查都无从谈起。
CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    character_id    TEXT NOT NULL,
    relationship_id TEXT,
    conversation_id TEXT NOT NULL,           -- 只作为来源记录，不作为记忆分区主键
    activity_id     TEXT,
    domain          TEXT NOT NULL DEFAULT 'real',
    speaker         TEXT NOT NULL,           -- user / character / system
    content         TEXT NOT NULL,
    occurred_at     TEXT NOT NULL,           -- 事件时间（这件事何时发生）
    occurred_precision TEXT NOT NULL DEFAULT 'exact',
    source_timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    recorded_at     TEXT NOT NULL,           -- 记录时间（何时写进库）
    turn_index      INTEGER,
    -- 幂等键：同一轮消息被重复提交时不会写两条
    idempotency_key TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_scope_time
    ON events (user_id, character_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_activity
    ON events (activity_id, occurred_at);
-- 幂等属于同一用户、角色与领域；不同活动/关系可重复使用客户端消息键。
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_idempotency_scope
    ON events (user_id, character_id, domain,
               COALESCE(relationship_id, ''), COALESCE(activity_id, ''), idempotency_key);

-- ----------------------------------------------------------------------------
-- 四类记忆：共用一张表
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memories (
    memory_id       TEXT PRIMARY KEY,
    memory_type     TEXT NOT NULL
                         CHECK (memory_type IN ('working', 'episodic', 'semantic', 'procedural')),
    schema_version  INTEGER NOT NULL DEFAULT 1,

    -- 作用域
    user_id         TEXT NOT NULL,
    character_id    TEXT NOT NULL,
    relationship_id TEXT,
    activity_id     TEXT,
    domain          TEXT NOT NULL DEFAULT 'real',

    -- 来源（provenance）
    source_event_ids_json TEXT NOT NULL DEFAULT '[]',
    source_speaker  TEXT,
    extraction_version TEXT NOT NULL DEFAULT 'v1',
    candidate_key   TEXT,
    -- 去重键 = source_event_id + extractor_version + candidate_key，
    -- 重跑抽取时靠它保证幂等（UNIQUE 允许多个 NULL）
    dedupe_key      TEXT UNIQUE,

    -- 认识论
    epistemic_status TEXT NOT NULL
                         CHECK (epistemic_status IN
                                ('explicit_setting', 'user_report',
                                 'observed_interaction', 'inferred')),
    modality        TEXT NOT NULL
                         CHECK (modality IN
                                ('occurred', 'intention', 'hypothetical', 'quoted', 'fictional')),

    -- 时间：occurred_* 是事件时间，valid_* 是事实有效期，两者不要混用
    occurred_start  TEXT,
    occurred_end    TEXT,
    occurred_precision TEXT NOT NULL DEFAULT 'unknown',
    valid_from      TEXT,
    valid_to        TEXT,      -- 为空表示至今仍然成立
    supersedes_id   TEXT,

    status          TEXT NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active', 'superseded', 'retracted', 'deleted')),

    -- 权重
    importance      REAL NOT NULL DEFAULT 0.5,
    retrieval_strength REAL NOT NULL DEFAULT 0.5,
    last_used_at    TEXT,
    access_count    INTEGER NOT NULL DEFAULT 0,

    -- 正文：结构化 JSON + 供关键词检索的扁平文本
    body_json       TEXT NOT NULL,
    body_text       TEXT NOT NULL DEFAULT '',

    recorded_at     TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memories_scope
    ON memories (user_id, character_id, memory_type, status);
CREATE INDEX IF NOT EXISTS idx_memories_occurred
    ON memories (user_id, character_id, occurred_start DESC);
CREATE INDEX IF NOT EXISTS idx_memories_recorded
    ON memories (user_id, character_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_activity
    ON memories (activity_id, memory_type);

-- ----------------------------------------------------------------------------
-- 记忆之间的链接：支持、派生、取代、组成
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_links (
    link_id        TEXT PRIMARY KEY,
    from_memory_id TEXT NOT NULL,
    to_memory_id   TEXT NOT NULL,
    relation       TEXT NOT NULL
                        CHECK (relation IN ('supports', 'derived_from', 'supersedes', 'part_of')),
    created_at     TEXT NOT NULL,
    UNIQUE (from_memory_id, to_memory_id, relation)
);

CREATE INDEX IF NOT EXISTS idx_links_to   ON memory_links (to_memory_id, relation);
CREATE INDEX IF NOT EXISTS idx_links_from ON memory_links (from_memory_id, relation);

-- ----------------------------------------------------------------------------
-- 向量索引：语义通道的候选来源
-- ----------------------------------------------------------------------------
-- 这张表是**派生数据**，和 FTS 索引一样可以随时从 memories 重建。
-- vector 列存的是 float32 数组的原始字节（array('f').tobytes()），
-- 比 JSON 文本省空间，读出来用 array('f', blob) 即可还原。
-- model 记录是哪个嵌入模型算出来的：换模型后旧向量不会混进新检索，
-- 重建时按 model 过滤，不会误用。
CREATE TABLE IF NOT EXISTS memory_vectors (
    memory_id  TEXT PRIMARY KEY,
    model      TEXT NOT NULL,
    dim        INTEGER NOT NULL,
    vector     BLOB NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_vectors_model ON memory_vectors (model);

-- ----------------------------------------------------------------------------
-- 主题摘要：一段时期内若干记忆的索引卡
-- ----------------------------------------------------------------------------
-- 设计要点：摘要只做索引，不做事实底座。
-- digest 是一句概述，真正的内容仍然在 source_memory_ids 指向的记忆里。
-- 主题在精确的作用域内唯一：重建摘要是「更新同一张卡」，不是「再发一张卡」，
-- 这样外部（宿主、日志）引用的 summary_id 不会失效。
CREATE TABLE IF NOT EXISTS summaries (
    summary_id      TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    character_id    TEXT NOT NULL,
    relationship_id TEXT,
    activity_id     TEXT,
    domain          TEXT NOT NULL DEFAULT 'real',
    scope_key       TEXT NOT NULL,
    topic           TEXT NOT NULL,
    digest          TEXT NOT NULL,
    source_memory_ids_json TEXT NOT NULL DEFAULT '[]',
    source_event_ids_json  TEXT NOT NULL DEFAULT '[]',
    span_start      TEXT,
    span_end        TEXT,
    importance      REAL NOT NULL DEFAULT 0.5,
    status          TEXT NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active', 'stale')),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (scope_key, topic)
);

CREATE INDEX IF NOT EXISTS idx_summaries_scope
    ON summaries (user_id, character_id, status, importance DESC);

-- ----------------------------------------------------------------------------
-- 异步整理任务：抽取、去重、归纳、核心更新候选
-- ----------------------------------------------------------------------------
-- 架构文档第 6 节：每轮先把消息、检查点、整理任务写进同一个事务，再异步处理。
-- 这样即使整理失败或进程被杀，任务也不会丢，可以重试。
CREATE TABLE IF NOT EXISTS consolidation_jobs (
    job_id         TEXT PRIMARY KEY,
    job_type       TEXT NOT NULL
                        CHECK (job_type IN ('extract', 'dedupe', 'summarize', 'propose_core')),
    user_id        TEXT NOT NULL,
    character_id   TEXT NOT NULL,
    relationship_id TEXT,
    activity_id    TEXT,
    domain         TEXT NOT NULL DEFAULT 'real',
    payload_json   TEXT NOT NULL DEFAULT '{}',
    status         TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'running', 'done', 'failed')),
    attempts       INTEGER NOT NULL DEFAULT 0,
    max_attempts   INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    available_at   TEXT NOT NULL DEFAULT '1970-01-01T00:00:00.000000+00:00',
    claim_token    TEXT,
    lease_until    TEXT,
    last_error     TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status
    ON consolidation_jobs (status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_available
    ON consolidation_jobs (status, available_at, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_lease
    ON consolidation_jobs (status, lease_until);
