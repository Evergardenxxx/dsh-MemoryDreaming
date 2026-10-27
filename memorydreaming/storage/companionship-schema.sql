-- 相处偏好是经用户确认的场景投影；推测先保留候选，不改人格核心。
CREATE TABLE IF NOT EXISTS companionship_proposals (
    proposal_id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    user_id TEXT NOT NULL,
    character_id TEXT NOT NULL,
    relationship_id TEXT,
    activity_id TEXT,
    domain TEXT NOT NULL,
    preference_key TEXT NOT NULL,
    context TEXT NOT NULL,
    value_json TEXT,
    evidence_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    base_version INTEGER NOT NULL,
    content_key TEXT NOT NULL,
    request_key TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','confirmed','rejected','invalidated')),
    approval_event_json TEXT,
    decision_key TEXT,
    applied_version INTEGER,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decided_at TEXT,
    UNIQUE(scope_key,idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_companionship_proposals_content
    ON companionship_proposals(scope_key,content_key);

CREATE TABLE IF NOT EXISTS companionship_preferences (
    scope_key TEXT NOT NULL,
    user_id TEXT NOT NULL,
    character_id TEXT NOT NULL,
    relationship_id TEXT,
    activity_id TEXT,
    domain TEXT NOT NULL,
    preference_key TEXT NOT NULL,
    context TEXT NOT NULL,
    version INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('confirmed','revoked','invalidated')),
    value_json TEXT,
    evidence_json TEXT NOT NULL,
    approval_event_json TEXT NOT NULL,
    source_proposal_id TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(scope_key,preference_key,context)
);

CREATE TABLE IF NOT EXISTS companionship_history (
    scope_key TEXT NOT NULL,
    user_id TEXT NOT NULL,
    character_id TEXT NOT NULL,
    relationship_id TEXT,
    activity_id TEXT,
    domain TEXT NOT NULL,
    preference_key TEXT NOT NULL,
    context TEXT NOT NULL,
    version INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('confirmed','revoked','invalidated')),
    value_json TEXT,
    evidence_json TEXT NOT NULL,
    approval_event_json TEXT NOT NULL,
    source_proposal_id TEXT,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope_key,preference_key,context,version)
);

-- 终态回执只记录操作元数据，不重复保存偏好正文。
CREATE TABLE IF NOT EXISTS companionship_receipts (
    scope_key TEXT NOT NULL,
    user_id TEXT NOT NULL,
    character_id TEXT NOT NULL,
    relationship_id TEXT,
    activity_id TEXT,
    domain TEXT NOT NULL,
    receipt_id TEXT NOT NULL,
    request_key TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope_key,receipt_id)
);
