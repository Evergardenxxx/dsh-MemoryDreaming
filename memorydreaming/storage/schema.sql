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
-- scope_key 区分领域及关系；身份跨同领域关系共享，关系档案独立版本化。
-- 版本号在同一核心范围内递增，配合 expected_version 防止窗口间覆盖。
CREATE TABLE IF NOT EXISTS core_versions (
    scope_key      TEXT NOT NULL,
    user_id        TEXT    NOT NULL,
    character_id   TEXT    NOT NULL,
    kind           TEXT    NOT NULL CHECK (kind IN ('identity', 'relationship')),
    core_version   INTEGER NOT NULL,
    relationship_id TEXT,
    domain         TEXT NOT NULL DEFAULT 'real',
    payload_json   TEXT    NOT NULL,
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    field_evidence_json TEXT NOT NULL DEFAULT '{}',
    change_reason  TEXT    NOT NULL,
    -- active=当前生效；superseded=被新版本取代（仍可回退）；rejected=候选被否决
    status         TEXT    NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active', 'superseded', 'rejected')),
    effective_from TEXT    NOT NULL,
    created_at     TEXT    NOT NULL,
    PRIMARY KEY (scope_key, kind, core_version)
);

CREATE INDEX IF NOT EXISTS idx_core_lookup
    ON core_versions (scope_key, kind, core_version DESC);

CREATE TABLE IF NOT EXISTS core_field_revisions (
    scope_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    core_version INTEGER NOT NULL,
    field_name TEXT NOT NULL,
    value_json TEXT,
    evidence_json TEXT NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    effective_from TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY(scope_key,kind,core_version,field_name)
);

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

-- 上下文压缩/窗口结束时的短交接。它是工作状态的宿主投影，不是新的事实来源。
-- payload_json 只存下一窗口需要的结构化 briefing；完整历史仍在 events/memories。
CREATE TABLE IF NOT EXISTS session_handoffs (
    handoff_id       TEXT PRIMARY KEY,
    scope_key        TEXT NOT NULL,
    user_id          TEXT NOT NULL,
    character_id     TEXT NOT NULL,
    relationship_id  TEXT,
    domain           TEXT NOT NULL DEFAULT 'real',
    conversation_id  TEXT NOT NULL,
    activity_id      TEXT,
    source_event_id  TEXT,
    activity_version INTEGER,
    payload_json     TEXT NOT NULL DEFAULT '{}',
    idempotency_key  TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active', 'superseded')),
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (scope_key, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_handoffs_latest
    ON session_handoffs (user_id, character_id, relationship_id, domain,
                         activity_id, status, updated_at DESC);

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

-- 事件级遗忘账本。正文行保留以维护外键和审计结构，但对外读取、重新抽取
-- 和来源支持都必须先检查这张表；物理备份/供应商历史清理属于更高等级操作。
CREATE TABLE IF NOT EXISTS forgotten_events (
    event_id    TEXT PRIMARY KEY,
    scope_key   TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_forgotten_events_scope ON forgotten_events (scope_key);

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
    source_spans_json TEXT NOT NULL DEFAULT '{}',
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

-- 记忆的系统时间修订日志。当前 memories 行是最新投影；这里保留每次
-- 内容/效力/状态变化前后的快照，使 knowledge_at 与 fact_at 可以分开查询。
CREATE TABLE IF NOT EXISTS memory_revisions (
    revision_id    TEXT PRIMARY KEY,
    memory_id      TEXT NOT NULL,
    revision       INTEGER NOT NULL,
    user_id        TEXT NOT NULL,
    character_id   TEXT NOT NULL,
    relationship_id TEXT,
    activity_id    TEXT,
    domain         TEXT NOT NULL DEFAULT 'real',
    memory_type    TEXT NOT NULL,
    status         TEXT NOT NULL,
    valid_from     TEXT,
    valid_to       TEXT,
    snapshot_json  TEXT NOT NULL,
    system_from    TEXT NOT NULL,
    system_to      TEXT,
    created_at     TEXT NOT NULL,
    UNIQUE(memory_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_memory_revisions_lookup
    ON memory_revisions (user_id, character_id, domain, memory_id, system_from DESC);

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

-- 一个组内 AND，多个组之间 OR；空组无效，不产生无依据的事实根。
CREATE TABLE IF NOT EXISTS evidence_sets (
    memory_id TEXT PRIMARY KEY REFERENCES memories(memory_id),
    scope_key TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version > 0),
    content_key TEXT NOT NULL,
    reason TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_groups (
    group_id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL REFERENCES evidence_sets(memory_id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    UNIQUE(memory_id, position)
);
CREATE TABLE IF NOT EXISTS evidence_members (
    group_id TEXT NOT NULL REFERENCES evidence_groups(group_id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('event','memory','core_version')),
    target TEXT NOT NULL,
    revision TEXT NOT NULL,
    PRIMARY KEY(group_id, position),
    UNIQUE(group_id, kind, target)
);
CREATE INDEX IF NOT EXISTS idx_evidence_target ON evidence_members(kind,target);

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
    source_digest   TEXT NOT NULL DEFAULT '',
    algorithm_version TEXT NOT NULL DEFAULT 'extractive-v1',
    span_start      TEXT,
    span_end        TEXT,
    importance      REAL NOT NULL DEFAULT 0.5,
    status          TEXT NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active', 'stale', 'withdrawn', 'rolled_back')),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (scope_key, topic)
);

CREATE INDEX IF NOT EXISTS idx_summaries_scope
    ON summaries (user_id, character_id, status, importance DESC);

CREATE TABLE IF NOT EXISTS summary_revisions (
    summary_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    character_id TEXT NOT NULL,
    relationship_id TEXT,
    activity_id TEXT,
    domain TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    operation TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY(summary_id,revision)
);
CREATE TABLE IF NOT EXISTS summary_restore_receipts (
    scope_key TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_key TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope_key,idempotency_key)
);

CREATE TABLE IF NOT EXISTS proposal_batches (
    batch_id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_key TEXT NOT NULL,
    request_event_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    items_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    reviewer TEXT,
    decision_reason TEXT,
    receipt_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(scope_key,idempotency_key)
);

-- 运维只保存计数/耗时与固定告警码，不保存查询或记忆正文。
CREATE TABLE IF NOT EXISTS operational_metrics (
    scope_key TEXT NOT NULL,
    user_id TEXT NOT NULL,
    character_id TEXT NOT NULL,
    relationship_id TEXT,
    activity_id TEXT,
    domain TEXT NOT NULL,
    bucket TEXT NOT NULL,
    metric TEXT NOT NULL,
    count INTEGER NOT NULL,
    total_ms REAL NOT NULL DEFAULT 0,
    max_ms REAL NOT NULL DEFAULT 0,
    PRIMARY KEY(scope_key,bucket,metric)
);
CREATE TABLE IF NOT EXISTS operational_alerts (
    scope_key TEXT NOT NULL,
    code TEXT NOT NULL,
    severity TEXT NOT NULL,
    state TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    resolved_at TEXT,
    PRIMARY KEY(scope_key,code)
);
CREATE TABLE IF NOT EXISTS provider_usage (
    request_token TEXT PRIMARY KEY,
    provider_key TEXT NOT NULL,
    started_at TEXT NOT NULL,
    reserved_tokens INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_provider_usage_window ON provider_usage(provider_key,started_at);
CREATE TABLE IF NOT EXISTS provider_budgets (
    provider_key TEXT PRIMARY KEY,
    rpm INTEGER NOT NULL,
    tpm INTEGER NOT NULL
);

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

-- 遗忘只存 HMAC 指纹和来源位置，不在账本里复制被遗忘正文。
CREATE TABLE IF NOT EXISTS memory_tombstones (
    tombstone_id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    assertion_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(scope_key, assertion_key)
);
CREATE TABLE IF NOT EXISTS tombstone_sources (
    tombstone_id TEXT NOT NULL REFERENCES memory_tombstones(tombstone_id),
    event_id TEXT NOT NULL,
    span_start INTEGER,
    span_end INTEGER,
    UNIQUE(tombstone_id, event_id, span_start, span_end)
);
CREATE INDEX IF NOT EXISTS idx_tombstone_event ON tombstone_sources(event_id);
-- 明确重新学习只授权一条带新用户来源的记录；不撤销旧来源的抑制。
CREATE TABLE IF NOT EXISTS fact_forgetting_barriers (
    tombstone_id TEXT PRIMARY KEY REFERENCES memory_tombstones(tombstone_id),
    scope_key TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK(generation > 0),
    forgotten_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS relearning_grants (
    grant_id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    tombstone_id TEXT NOT NULL REFERENCES memory_tombstones(tombstone_id),
    forgetting_generation INTEGER NOT NULL,
    original_memory_id TEXT NOT NULL REFERENCES memories(memory_id),
    memory_id TEXT NOT NULL UNIQUE REFERENCES memories(memory_id) DEFERRABLE INITIALLY DEFERRED,
    confirmation_event_id TEXT NOT NULL REFERENCES events(event_id),
    content_key TEXT NOT NULL,
    confirmation_key TEXT NOT NULL,
    request_key TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(scope_key, idempotency_key),
    UNIQUE(tombstone_id, forgetting_generation, confirmation_event_id)
);
CREATE TABLE IF NOT EXISTS scope_mutations (
    scope_key TEXT PRIMARY KEY,
    epoch INTEGER NOT NULL DEFAULT 0
);

-- 作用域级物理清除账本。它不复制被清除正文，只记录代次与原因摘要，
-- 供旧快照导入预检阻止已经清除的作用域重新上线。
CREATE TABLE IF NOT EXISTS scope_purges (
    scope_key       TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    character_id    TEXT NOT NULL,
    relationship_id TEXT,
    activity_id     TEXT,
    domain          TEXT NOT NULL DEFAULT 'real',
    purge_generation INTEGER NOT NULL,
    reason          TEXT NOT NULL DEFAULT '',
    purged_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scope_purges_identity
    ON scope_purges (user_id, character_id, domain, purged_at DESC);

-- 只保存不含正文的运维健康快照，用于趋势和告警；不会进入模型上下文。
CREATE TABLE IF NOT EXISTS health_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    user_id TEXT NOT NULL,
    character_id TEXT NOT NULL,
    relationship_id TEXT,
    activity_id TEXT,
    domain TEXT NOT NULL DEFAULT 'real',
    payload_json TEXT NOT NULL,
    captured_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_health_snapshots_scope
    ON health_snapshots (user_id, character_id, domain, captured_at DESC);

-- 跨进程检索缓存只保存候选 ID 和版本指纹，不保存记忆正文。
-- 命中后仍需经过当前会话的 ViewReader 逐条复核；作用域代次或任一来源变化即失效。
-- storage-local 内容代次由数据库触发器递增，不随便携快照导出或导入。
CREATE TABLE IF NOT EXISTS retrieval_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    generation INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO retrieval_state (singleton, generation) VALUES (1, 0);

CREATE TABLE IF NOT EXISTS retrieval_cache (
    cache_key TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    current_event_id TEXT,
    mutation_epoch INTEGER NOT NULL DEFAULT 0,
    content_generation INTEGER NOT NULL DEFAULT 0,
    memory_ids_json TEXT NOT NULL DEFAULT '[]',
    summary_ids_json TEXT NOT NULL DEFAULT '[]',
    memory_revisions_json TEXT NOT NULL DEFAULT '{}',
    summary_revisions_json TEXT NOT NULL DEFAULT '{}',
    retrieval_complete INTEGER NOT NULL DEFAULT 1 CHECK (retrieval_complete IN (0,1)),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_retrieval_cache_scope_expiry
    ON retrieval_cache (scope_key, expires_at);
CREATE INDEX IF NOT EXISTS idx_retrieval_cache_expiry ON retrieval_cache (expires_at);

-- 实体索引是可重建投影，不承担事实真值。canonical_name 用于检索，
-- display_name 保留最近一次出现的表面形式；正文仍只在 memories 中保存。
CREATE TABLE IF NOT EXISTS entities (
    entity_id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    user_id TEXT NOT NULL,
    character_id TEXT NOT NULL,
    relationship_id TEXT,
    activity_id TEXT,
    domain TEXT NOT NULL DEFAULT 'real',
    canonical_name TEXT NOT NULL,
    display_name TEXT NOT NULL,
    entity_type TEXT NOT NULL DEFAULT 'mention',
    mention_count INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT,
    last_seen TEXT,
    UNIQUE(scope_key, canonical_name)
);
CREATE INDEX IF NOT EXISTS idx_entities_scope_name
    ON entities (user_id, character_id, domain, canonical_name);
CREATE TABLE IF NOT EXISTS entity_mentions (
    entity_id TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    role TEXT NOT NULL,
    surface TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(entity_id, memory_id, role)
);
CREATE INDEX IF NOT EXISTS idx_entity_mentions_memory ON entity_mentions(memory_id);
CREATE TABLE IF NOT EXISTS core_field_tombstones (
    scope_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    field_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope_key, kind, field_key)
);

-- 只保存宿主确认已显示的回复；未知投递状态不当作已发生的对话。
CREATE TABLE IF NOT EXISTS host_deliveries (
    event_id TEXT PRIMARY KEY REFERENCES events(event_id),
    user_event_id TEXT NOT NULL REFERENCES events(event_id),
    delivery_state TEXT NOT NULL CHECK(delivery_state IN ('final', 'interrupted')),
    used_memory_ids_json TEXT NOT NULL DEFAULT '[]',
    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    evidence_complete INTEGER NOT NULL DEFAULT 1 CHECK(evidence_complete IN (0,1))
);

-- 持久审计链：只记录状态变更的元数据和摘要指纹，不复制对话正文。
-- sequence + prev_hash + entry_hash 组成单调追加的全局链；任何历史行被改写、
-- 删除或插入都会在 verify 时暴露。幂等键只由仓储层按作用域和事件类型解释。
CREATE TABLE IF NOT EXISTS journal_entries (
    journal_id      TEXT PRIMARY KEY,
    sequence        INTEGER NOT NULL UNIQUE,
    scope_key       TEXT NOT NULL DEFAULT '',
    user_id         TEXT,
    character_id    TEXT,
    relationship_id TEXT,
    activity_id     TEXT,
    domain          TEXT,
    event_type      TEXT NOT NULL,
    entity_id       TEXT,
    idempotency_key TEXT,
    payload_json    TEXT NOT NULL DEFAULT '{}',
    prev_hash       TEXT NOT NULL,
    entry_hash      TEXT NOT NULL UNIQUE,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_journal_scope_time
    ON journal_entries (scope_key, sequence DESC);
CREATE INDEX IF NOT EXISTS idx_journal_type_time
    ON journal_entries (event_type, sequence DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_journal_idempotency
    ON journal_entries (scope_key, event_type, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- 核心建议与生效记录分开；终态记录用于幂等审批和回执查询。
CREATE TABLE IF NOT EXISTS core_proposals (
    proposal_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE,
    scope_key TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    request_event_id TEXT NOT NULL REFERENCES events(event_id),
    target_kind TEXT NOT NULL CHECK(target_kind IN ('identity','relationship')),
    expected_version INTEGER NOT NULL,
    target_revision TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending_validation','applied','rejected','invalidated')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decision_code TEXT,
    reviewer TEXT,
    decision_reason TEXT,
    decided_at TEXT,
    applied_version INTEGER
);
CREATE INDEX IF NOT EXISTS idx_core_proposals_scope ON core_proposals(scope_key,state,expires_at);

-- 一般事实/遗忘/检查点提议。模型只能创建 pending_validation，可信宿主
-- 必须再次核对本轮真实用户事件、引用版本和作用域后才能执行。
CREATE TABLE IF NOT EXISTS change_proposals (
    proposal_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE,
    scope_key TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    request_event_id TEXT NOT NULL REFERENCES events(event_id),
    operation TEXT NOT NULL CHECK(operation IN ('correct_fact','forget_fact','forget_event','forget_scope','checkpoint')),
    target_refs_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    payload_json TEXT NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending_validation','applied','rejected','invalidated')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decision_code TEXT,
    reviewer TEXT,
    decision_reason TEXT,
    decided_at TEXT,
    receipt_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_change_proposals_scope ON change_proposals(scope_key,state,expires_at);

-- scope purge 会删除请求事件及提议本身。独立最小回执不得引用已清除事件，
-- 只用 HMAC 绑定相同审批重试；不保存正文、替换体或审批原话。
CREATE TABLE IF NOT EXISTS change_purge_receipts (
    proposal_id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    operation_id TEXT NOT NULL UNIQUE,
    decision_fingerprint TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    receipt_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_change_purge_receipts_scope ON change_purge_receipts(scope_key);

-- 运行调度状态：与用户 scope 无关；不含端点、API key、事件或正文，不导出。
CREATE TABLE IF NOT EXISTS provider_limits (
    provider_key TEXT PRIMARY KEY,
    max_concurrent INTEGER NOT NULL CHECK(max_concurrent BETWEEN 1 AND 32),
    min_interval_seconds REAL NOT NULL CHECK(min_interval_seconds BETWEEN 0 AND 3600),
    not_before TEXT NOT NULL,
    next_start_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_requests (
    request_token TEXT PRIMARY KEY,
    provider_key TEXT NOT NULL REFERENCES provider_limits(provider_key),
    started_at TEXT NOT NULL,
    lease_until TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_provider_requests_lease ON provider_requests(provider_key,lease_until);
