-- Restricted login enrollment authority. No plaintext secrets or bearer tokens.
CREATE TABLE IF NOT EXISTS mfa_enrollments (
    jti TEXT NOT NULL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at DOUBLE PRECISION NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    encrypted_pending_secret TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    consumed_at DOUBLE PRECISION,
    CONSTRAINT mfa_enrollment_jti_nonempty CHECK (length(jti) > 0),
    CONSTRAINT mfa_enrollment_expiry_bound CHECK (expires_at > 0 AND expires_at <= 253402300799),
    CONSTRAINT mfa_enrollment_state CHECK (state IN ('pending', 'consumed', 'cancelled')),
    CONSTRAINT mfa_enrollment_terminal_secret CHECK (state = 'pending' OR encrypted_pending_secret IS NULL)
);
CREATE INDEX IF NOT EXISTS idx_mfa_enrollments_user ON mfa_enrollments(user_id);
CREATE INDEX IF NOT EXISTS idx_mfa_enrollments_expiry ON mfa_enrollments(expires_at);
