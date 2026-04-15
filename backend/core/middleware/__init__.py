"""O.D.I.N. — HTTP middleware components.

v1.8.9 introduces the agent-surface middleware set:
- `idempotency` — caches mutating-request responses keyed on
  (Idempotency-Key, user_id) for 24h so agents can safely retry.
- `dry_run` — honors `X-Dry-Run: true` on mutating requests so
  agents can preview what a call would do without committing.

The middleware is registered via the app factory (`core/app.py`).
"""
