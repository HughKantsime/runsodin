# Spec: WebSocket Token Scoping + MFA Hardening

## Goal

Fix two focused auth hardening items found in the session security audit: ws-tokens must be rejected by REST endpoints, and MFA pending tokens must be blacklisted after use.

## Item 1: ws-token Must Not Be Accepted on REST Endpoints (`backend/deps.py`)

### Problem
The `/auth/ws-token` endpoint issues a JWT with `"ws": True` in the payload. This claim is written but never read. `get_current_user()` in `deps.py` accepts any valid JWT — including ws-tokens — for REST API access. A ws-token captured from a WebSocket URL (which appears in server access logs as a query parameter) can be replayed against any REST endpoint for its 5-minute lifetime.

### Fix
In `get_current_user()` in `backend/deps.py`, after decoding the JWT payload and before returning the user dict, add:

```python
if payload.get("ws"):
    return None  # ws-tokens are not valid for REST API access
```

This applies to all three auth paths in `get_current_user()`:
- Cookie auth (Try 0)
- Bearer token auth (Try 1)

The WebSocket endpoint in `main.py` uses `decode_token()` directly (not `get_current_user()`), so it is unaffected by this change and will continue to accept ws-tokens.

## Item 2: Blacklist MFA Pending Token After Successful Verification (`backend/routers/auth.py`)

### Problem
The `mfa_pending` JWT used to complete 2FA is never blacklisted after a successful verification. It remains valid until its 5-minute expiry. Within that window, the same `mfa_pending` token can be submitted again with a different TOTP code to obtain a second full session token.

The `mfa_pending` claim correctly blocks access to protected REST endpoints (checked in `deps.py`), so this is not a direct privilege escalation. However, it allows a second session to be issued from a single MFA flow.

### Fix
In the `mfa_verify` endpoint (`backend/routers/auth.py`), after successful verification and before returning the new session token:

1. Extract the `jti` from the decoded `mfa_pending` token payload
2. Insert it into `token_blacklist` with the token's `expires_at`

Look at how the logout endpoint blacklists tokens (`routers/auth.py` — search for `token_blacklist INSERT`) to use the exact same pattern.

```python
# After verify succeeds, blacklist the mfa_pending token
mfa_jti = mfa_payload.get("jti")
if mfa_jti:
    mfa_exp = mfa_payload.get("exp", 0)
    db.execute(text("""
        INSERT OR IGNORE INTO token_blacklist (jti, expires_at)
        VALUES (:jti, :exp)
    """), {"jti": mfa_jti, "exp": datetime.fromtimestamp(mfa_exp, tz=timezone.utc).isoformat()})
    db.commit()
```

## Item 3: Fix `revoke_all_sessions` to Handle Cookie Auth Callers (`backend/routers/auth.py`)

### Problem
The "revoke all other sessions" endpoint extracts the caller's current session JTI from the `Authorization: Bearer` header only. Cookie-auth callers (browsers) have no Bearer header — `current_jti` stays as `None`, and the WHERE clause `token_jti != ""` always matches all JTIs (including the caller's own), so the caller gets logged out.

### Fix
Extract the current JTI from whichever auth method was used — check cookie first, then Bearer:

```python
# Try cookie first
session_token = request.cookies.get("session")
auth_header = request.headers.get("authorization", "")
raw_token = session_token or (auth_header.split(" ", 1)[1] if auth_header.startswith("Bearer ") else None)
current_jti = None
if raw_token:
    try:
        payload = _jwt.decode(raw_token, auth_module.SECRET_KEY, algorithms=[auth_module.ALGORITHM])
        current_jti = payload.get("jti")
    except Exception:
        pass
```

Apply the same fix to the `is_current` logic in `list_sessions`.

## Acceptance Criteria

- [ ] ws-tokens rejected by `get_current_user()` (REST endpoints return 401/403 when ws-token used)
- [ ] WebSocket endpoint still accepts ws-tokens
- [ ] MFA pending token is in `token_blacklist` after successful MFA verification
- [ ] Re-submitting the same mfa_pending token returns 401 (blacklisted)
- [ ] `revoke_all_sessions` does not log out the caller when using cookie auth
- [ ] `make test` passes

## Technical Notes

- `get_current_user()` is in `backend/deps.py` — the ws-token check goes in each auth Try block
- `mfa_verify` is in `backend/routers/auth.py` — search for `mfa_token`, `mfa_pending`
- `revoke_all_sessions` is in `backend/routers/auth.py` — search for `revoke_all` or `DELETE FROM active_sessions WHERE user_id`
- The `_jwt` import in auth.py — confirm the module name (may be `import jwt as _jwt`)
