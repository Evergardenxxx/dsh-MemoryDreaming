-- 表达与习惯成长是已批准的角色投影，不自动改核心、原始事件或事实。
CREATE TABLE IF NOT EXISTS growth_proposals (
    proposal_id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    user_id TEXT NOT NULL,
    character_id TEXT NOT NULL,
    relationship_id TEXT,
    activity_id TEXT,
    domain TEXT NOT NULL,
    request_id TEXT NOT NULL,
    request_key TEXT NOT NULL,
    content_key TEXT NOT NULL,
    field TEXT NOT NULL,
    value TEXT NOT NULL,
    sources_json TEXT NOT NULL,
    source_epoch INTEGER NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    UNIQUE(scope_key,request_id)
);
CREATE INDEX IF NOT EXISTS idx_growth_proposal_content ON growth_proposals(scope_key,content_key);
CREATE TABLE IF NOT EXISTS growth_values (
    scope_key TEXT NOT NULL,
    user_id TEXT NOT NULL,
    character_id TEXT NOT NULL,
    relationship_id TEXT,
    activity_id TEXT,
    domain TEXT NOT NULL,
    field TEXT NOT NULL,
    version INTEGER NOT NULL,
    value TEXT NOT NULL,
    sources_json TEXT NOT NULL,
    confirmation_json TEXT NOT NULL,
    source_epoch INTEGER NOT NULL,
    state TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(scope_key,field)
);
CREATE TABLE IF NOT EXISTS growth_history (
    scope_key TEXT NOT NULL,
    user_id TEXT NOT NULL,
    character_id TEXT NOT NULL,
    relationship_id TEXT,
    activity_id TEXT,
    domain TEXT NOT NULL,
    field TEXT NOT NULL,
    version INTEGER NOT NULL,
    value TEXT NOT NULL,
    sources_json TEXT NOT NULL,
    confirmation_json TEXT NOT NULL,
    source_epoch INTEGER NOT NULL,
    state TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope_key,field,version)
);
CREATE TABLE IF NOT EXISTS growth_receipts (
    scope_key TEXT NOT NULL,
    user_id TEXT NOT NULL,
    character_id TEXT NOT NULL,
    relationship_id TEXT,
    activity_id TEXT,
    domain TEXT NOT NULL,
    request_id TEXT NOT NULL,
    request_key TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope_key,request_id)
);
