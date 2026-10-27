-- 自传属于有来源、需确认的叙事投影，不是新的用户事实或人格根。
CREATE TABLE IF NOT EXISTS narrative_entries (
    entry_id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    user_id TEXT NOT NULL,
    character_id TEXT NOT NULL,
    relationship_id TEXT,
    activity_id TEXT,
    domain TEXT NOT NULL,
    perspective TEXT NOT NULL CHECK(perspective IN ('user','character','shared')),
    modality TEXT NOT NULL CHECK(modality IN ('occurred','shared_fiction')),
    title TEXT NOT NULL,
    text TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    sources_json TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version>0),
    state TEXT NOT NULL CHECK(state IN ('accepted','withdrawn','invalidated')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_narrative_timeline ON narrative_entries(scope_key,state,occurred_at);

CREATE TABLE IF NOT EXISTS narrative_proposals (
    proposal_id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    user_id TEXT NOT NULL,
    character_id TEXT NOT NULL,
    relationship_id TEXT,
    activity_id TEXT,
    domain TEXT NOT NULL,
    request_id TEXT NOT NULL,
    request_key TEXT NOT NULL,
    target_id TEXT,
    expected_version INTEGER NOT NULL,
    payload_json TEXT,
    sources_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('proposed','accepted','rejected','invalidated')),
    reviewer TEXT,
    decision_reason TEXT,
    receipt_json TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    UNIQUE(scope_key,request_id)
);

CREATE TABLE IF NOT EXISTS narrative_versions (
    entry_id TEXT NOT NULL REFERENCES narrative_entries(entry_id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    scope_key TEXT NOT NULL,
    user_id TEXT NOT NULL,
    character_id TEXT NOT NULL,
    relationship_id TEXT,
    activity_id TEXT,
    domain TEXT NOT NULL,
    snapshot_json TEXT,
    sources_json TEXT NOT NULL,
    operation TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(entry_id,version)
);
