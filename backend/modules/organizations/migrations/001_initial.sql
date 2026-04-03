-- organizations/migrations/001_initial.sql
-- Canonical source for organizations domain tables.
-- Depends on: core (users table must exist first).

CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(100) UNIQUE NOT NULL,
    description TEXT,
    owner_id INTEGER REFERENCES users(id),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    is_org BOOLEAN DEFAULT 0,
    branding_json TEXT,
    settings_json TEXT
);

CREATE TABLE IF NOT EXISTS oidc_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name TEXT DEFAULT 'SSO Login',
    client_id TEXT,
    client_secret_encrypted TEXT,
    tenant_id TEXT,
    discovery_url TEXT,
    scopes TEXT DEFAULT 'openid profile email',
    auto_create_users BOOLEAN DEFAULT 0,
    default_role TEXT DEFAULT 'viewer',
    is_enabled BOOLEAN DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS oidc_pending_states (
    state TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS oidc_auth_codes (
    code TEXT PRIMARY KEY,
    access_token TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quota_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    period_key VARCHAR(20) NOT NULL,
    grams_used REAL DEFAULT 0,
    hours_used REAL DEFAULT 0,
    jobs_used INTEGER DEFAULT 0,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, period_key)
);
