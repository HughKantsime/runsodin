-- Provider-neutral OIDC policy and one-time nonce persistence.

ALTER TABLE oidc_config ADD COLUMN provider_type TEXT NOT NULL DEFAULT 'microsoft';
ALTER TABLE oidc_config ADD COLUMN allowed_domains TEXT;
ALTER TABLE oidc_pending_states ADD COLUMN nonce TEXT NOT NULL DEFAULT '';
