-- Tenant-scoped Google Classroom read-only integration.

CREATE TABLE IF NOT EXISTS classroom_connections (
    org_id INTEGER PRIMARY KEY REFERENCES groups(id),
    client_id TEXT,
    client_secret_encrypted TEXT,
    allowed_domains TEXT,
    account_subject TEXT,
    account_email TEXT,
    granted_scopes TEXT,
    refresh_token_encrypted TEXT,
    access_token_encrypted TEXT,
    access_token_expires_at TEXT,
    state TEXT NOT NULL DEFAULT 'not_connected',
    last_success_at TEXT,
    last_error_code TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS classroom_oauth_states (
    state TEXT PRIMARY KEY,
    org_id INTEGER NOT NULL REFERENCES groups(id),
    admin_id INTEGER NOT NULL REFERENCES users(id),
    code_verifier_encrypted TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS classroom_course_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL REFERENCES groups(id),
    provider_course_id TEXT NOT NULL,
    cost_center_id INTEGER NOT NULL REFERENCES education_cost_centers(id),
    course_name TEXT NOT NULL,
    course_section TEXT,
    course_state TEXT,
    last_imported_at TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(org_id, provider_course_id),
    UNIQUE(org_id, cost_center_id)
);

CREATE TABLE IF NOT EXISTS classroom_roster_identities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL REFERENCES groups(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    provider_user_id TEXT NOT NULL,
    normalized_email TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active',
    last_seen_at TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(org_id, provider_user_id),
    UNIQUE(org_id, normalized_email)
);

CREATE INDEX IF NOT EXISTS ix_classroom_mappings_center
    ON classroom_course_mappings(cost_center_id);
CREATE INDEX IF NOT EXISTS ix_classroom_roster_user
    ON classroom_roster_identities(user_id);
