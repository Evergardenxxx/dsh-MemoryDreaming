-- 承诺是有来源、待确认的意向，不因模型说“答应了”自动成为用户义务。
CREATE TABLE IF NOT EXISTS intentions (
    intention_id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    user_id TEXT NOT NULL,
    character_id TEXT NOT NULL,
    relationship_id TEXT,
    activity_id TEXT,
    domain TEXT NOT NULL,
    request_id TEXT NOT NULL,
    request_key TEXT NOT NULL,
    text TEXT NOT NULL,
    owner TEXT NOT NULL CHECK(owner IN ('user','character','shared')),
    category TEXT NOT NULL CHECK(category IN ('promise','continuation','creative')),
    due_at TEXT,
    event_trigger TEXT,
    triggered_at TEXT,
    trigger_evidence_json TEXT,
    sources_json TEXT NOT NULL,
    confirmation_json TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL CHECK(state IN ('proposed','open','completed','cancelled','invalidated')),
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(scope_key,request_id)
);
CREATE INDEX IF NOT EXISTS idx_intentions_scope_state ON intentions(scope_key,state,due_at);
CREATE TABLE IF NOT EXISTS intention_revisions (
    intention_id TEXT NOT NULL REFERENCES intentions(intention_id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    scope_key TEXT NOT NULL,
    user_id TEXT NOT NULL,
    character_id TEXT NOT NULL,
    relationship_id TEXT,
    activity_id TEXT,
    domain TEXT NOT NULL,
    operation TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY(intention_id,version)
);
CREATE TABLE IF NOT EXISTS intention_operations (
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
CREATE TABLE IF NOT EXISTS proactive_policies (
    scope_key TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    character_id TEXT NOT NULL,
    relationship_id TEXT,
    activity_id TEXT,
    domain TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    version INTEGER NOT NULL,
    confirmation_json TEXT NOT NULL,
    timezone TEXT NOT NULL,
    quiet_start TEXT NOT NULL,
    quiet_end TEXT NOT NULL,
    min_interval_seconds INTEGER NOT NULL,
    max_per_day INTEGER NOT NULL,
    last_claimed_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proactive_deliveries (
    delivery_id TEXT PRIMARY KEY,
    intention_id TEXT NOT NULL REFERENCES intentions(intention_id) ON DELETE CASCADE,
    scope_key TEXT NOT NULL,
    user_id TEXT NOT NULL,
    character_id TEXT NOT NULL,
    relationship_id TEXT,
    activity_id TEXT,
    domain TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','claimed','delivered','dropped')),
    claim_token TEXT,
    worker_id TEXT,
    lease_until TEXT,
    reserved_day TEXT,
    policy_version INTEGER NOT NULL,
    intention_version INTEGER NOT NULL,
    available_at TEXT NOT NULL DEFAULT '1970-01-01T00:00:00.000000+00:00',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    UNIQUE(intention_id)
);
CREATE INDEX IF NOT EXISTS idx_proactive_delivery_scope ON proactive_deliveries(scope_key,state,lease_until);
CREATE TABLE IF NOT EXISTS proactive_ack_receipts (
    claim_token TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    user_id TEXT NOT NULL,
    character_id TEXT NOT NULL,
    relationship_id TEXT,
    activity_id TEXT,
    domain TEXT NOT NULL,
    request_key TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS intention_trigger_receipts (
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
