-- core/migrations/005_idempotency_keys.sql
-- v1.8.9 — agent-native surface primitives.
--
-- Adds `idempotency_keys` — cache table for the Idempotency-Key
-- middleware. Clients (MCP server, OpenClaw skill, humans using
-- curl) send `Idempotency-Key: <uuid>` on POST/PUT/PATCH/DELETE; the
-- middleware writes the response here and replays it on subsequent
-- requests within 24h.
--
-- Design notes (post-codex-review, 2026-04-14):
-- - PK is (key, user_id) so two users with the same (vanishingly
--   unlikely but non-zero) UUID collision don't cross-replay.
-- - `claim-before-execute` atomicity: the middleware INSERTs a
--   "pending" row (state='pending', response_status=0, response_body='')
--   BEFORE running the handler. A second concurrent request with the
--   same key collides on the PK, finds `state='pending'`, and returns
--   409 `idempotency_in_progress` instead of executing the mutation
--   a second time. The first request's handler runs, then the
--   middleware UPDATEs the row to state='complete' with the real
--   response. This closes the race codex pass 1 flagged (duplicate
--   execution under concurrent retries).
-- - request_hash pins the cached response to the exact request body;
--   re-using the same key with a different body is a client bug and
--   the middleware returns 409 `idempotency_conflict` rather than
--   silently replaying.
-- - created_at is indexed for the hourly TTL-prune task; 24h TTL is
--   enforced at read time as well so a stale row can't replay even
--   if the pruner hasn't run yet. Pending rows older than a short
--   watchdog window (90s) are also cleaned up so a crashed handler
--   can't block the key forever.
-- - response_body stored as TEXT (serialized JSON); loader must not
--   truncate. SQLite TEXT and Postgres TEXT are both unlimited.
--
-- NOT added in this migration: agent token scopes. The existing
-- `api_tokens.scopes` JSON-array column already handles per-token
-- scope lists. v1.8.9 defines two new well-known scope strings —
-- `agent:read` and `agent:write` — that get written into that
-- existing column via the token-creation UI. No schema change
-- required.

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    response_status INTEGER NOT NULL DEFAULT 0,
    response_body TEXT NOT NULL DEFAULT '',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (key, user_id)
);

CREATE INDEX IF NOT EXISTS ix_idempotency_keys_created_at
    ON idempotency_keys(created_at);
CREATE INDEX IF NOT EXISTS ix_idempotency_keys_state
    ON idempotency_keys(state);
