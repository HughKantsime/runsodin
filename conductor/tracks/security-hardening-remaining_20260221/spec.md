# Spec: Security Hardening — Remaining Quick Fixes

## Goal

Complete the remaining security hardening items identified during the pre-launch security audit. v1.3.57 shipped the critical endpoint and parsing fixes; this track covers the 7 quick wins that require no architectural decisions.

## Context

v1.3.57 shipped 10 critical/high security fixes (auth guards, SSRF blocklist, XXE prevention, ZIP bomb protection, path traversal, HSTS, error sanitization, camera URL sanitization, last-admin protection, api_key schema separation). This track finishes the remaining "no-design-required" items from the same audit.

## Requirements

### 1. JWT Secret Entropy Fix (`entrypoint.sh`)

Current code generates `JWT_SECRET_KEY` with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. This produces ~192 bits of entropy via base64url encoding. Replace with `secrets.token_bytes(32)` encoded as hex (256 bits, no encoding overhead):

```sh
JWT_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_bytes(32).hex())")
```

Also apply to `ENCRYPTION_KEY` generation if the same pattern is used.

### 2. Numeric Field Bounds in Pydantic Schemas (`backend/schemas.py`)

Add `ge`/`le` constraints using `Field(...)` on:
- `slot_count`: 1 ≤ n ≤ 256
- `priority`: 0 ≤ n ≤ 10
- `quantity` / `quantity_per_bed`: 1 ≤ n ≤ 10000
- Any other integer fields that could be weaponized with extreme values

### 3. Camera URL Validation Before go2rtc Config Write

When a camera URL is persisted (POST/PUT `/cameras`), validate the URL:
- Must be `rtsp://` or `rtsps://` scheme (or reject with 400)
- Strip any shell metacharacters (`; & | $ backtick`) from the URL before writing to go2rtc YAML
- Do NOT allow URLs that resolve to localhost/127.x/169.254.x (same SSRF blocklist as printers)

### 4. Webhook URL SSRF Validation

When saving webhook URLs (org settings, system webhook config), validate that the URL does not point to internal/localhost addresses. Reuse the same SSRF blocklist logic:
- Reject scheme anything other than `https://` or `http://`
- Reject hostnames resolving to loopback, link-local, or private RFC-1918 ranges

### 5. Audit Log — Password Changes

When `update_user` successfully changes a password, call `log_audit()` (or equivalent) with:
- `action = "user.password_changed"`
- `target_user_id`
- `actor_user_id`

### 6. Audit Log — Successful Logins

When `login` succeeds, emit an audit event:
- `action = "auth.login"`
- `user_id`
- `ip_address` from request

### 7. GDPR Data Export Completeness

The `/api/v1/users/{id}/export` endpoint must include:
- API tokens (token name, created_at, last_used_at, scopes — NOT the token value)
- Quota usage records (`quota_usage` table)
- Active sessions (session metadata — NOT session tokens)

## Acceptance Criteria

- [ ] `entrypoint.sh` uses `token_bytes(32).hex()` for JWT secret generation
- [ ] `slot_count`, `priority`, `quantity` have enforced numeric bounds in schemas
- [ ] Camera create/update rejects non-rtsp schemes and strips shell metacharacters
- [ ] Camera URLs pointing to localhost/loopback are rejected with 400
- [ ] Webhook URLs pointing to loopback/private ranges are rejected
- [ ] `log_audit()` called on password change
- [ ] `log_audit()` called on successful login with IP
- [ ] GDPR export includes API tokens, quota_usage, active_sessions metadata

## Out of Scope

- localStorage → httpOnly cookies migration (architectural, needs separate design)
- go2rtc network binding change (infrastructure, needs testing)
- Container non-root user (Dockerfile change, separate track)
- Global rate limiting via slowapi (separate track)
- API token scope enforcement (separate track)

## Technical Notes

- Audit log helper: look for `log_audit()` or similar in `routers/auth.py` and `deps.py`
- SSRF blocklist: factor the logic from `routers/printers.py:_check_ssrf_blocklist()` into a shared utility in `deps.py` or a new `utils.py`
- go2rtc config write: search for where camera URLs are written to the go2rtc YAML (likely `routers/cameras.py` or a go2rtc helper)
- GDPR export: `GET /api/v1/users/{id}/export` in `routers/auth.py` or `routers/system.py`
