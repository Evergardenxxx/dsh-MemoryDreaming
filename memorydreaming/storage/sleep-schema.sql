-- 睡眠是生成派生物的生命周期，梦不进入四类事实记忆或核心。
CREATE TABLE IF NOT EXISTS sleep_runs (
    sleep_id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    user_id TEXT NOT NULL,
    character_id TEXT NOT NULL,
    relationship_id TEXT,
    activity_id TEXT,
    domain TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    request_key TEXT NOT NULL,
    scope_epoch INTEGER NOT NULL,
    seed TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('prepared','collecting','completed','invalidated')),
    inventory_json TEXT NOT NULL,
    completion_key TEXT,
    review_complete INTEGER NOT NULL DEFAULT 0,
    completion_json TEXT,
    invalidated_reason TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(scope_key,conversation_id,request_id)
);
CREATE INDEX IF NOT EXISTS idx_sleep_latest ON sleep_runs(scope_key,status,created_at DESC);
CREATE TABLE IF NOT EXISTS sleep_sources (
    sleep_id TEXT NOT NULL REFERENCES sleep_runs(sleep_id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('event','memory')),
    source_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    PRIMARY KEY(sleep_id,kind,source_id)
);
CREATE INDEX IF NOT EXISTS idx_sleep_source ON sleep_sources(kind,source_id);
CREATE TABLE IF NOT EXISTS sleep_batches (
    sleep_id TEXT NOT NULL REFERENCES sleep_runs(sleep_id) ON DELETE CASCADE,
    cursor INTEGER NOT NULL,
    fragments_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','reviewed')),
    claim_token TEXT,
    claim_request_id TEXT,
    lease_until TEXT,
    review_key TEXT,
    review_json TEXT,
    reviewed_at TEXT,
    PRIMARY KEY(sleep_id,cursor)
);
CREATE TABLE IF NOT EXISTS dreams (
    dream_id TEXT PRIMARY KEY,
    sleep_id TEXT NOT NULL UNIQUE REFERENCES sleep_runs(sleep_id) ON DELETE CASCADE,
    scope_key TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'dream' CHECK(kind='dream'),
    epistemic_status TEXT NOT NULL DEFAULT 'generated_fiction' CHECK(epistemic_status='generated_fiction'),
    modality TEXT NOT NULL DEFAULT 'fictional' CHECK(modality='fictional'),
    text TEXT NOT NULL,
    source_refs_json TEXT NOT NULL,
    generator TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','withdrawn')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sleep_weight_changes (
    sleep_id TEXT NOT NULL REFERENCES sleep_runs(sleep_id) ON DELETE CASCADE,
    memory_id TEXT NOT NULL,
    before_strength REAL NOT NULL,
    after_strength REAL NOT NULL,
    reason TEXT NOT NULL CHECK(reason IN ('source_replay','low_salience_decay')),
    restored_at TEXT,
    PRIMARY KEY(sleep_id,memory_id)
);
