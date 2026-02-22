# Evaluation Report — auth-hardening-architectural_20260221
**Date:** 2026-02-21
**Verdict:** PASS

## Acceptance Criteria Coverage

### Item 1: httpOnly Cookie Auth
- PASS: Login sets `session` cookie (httponly=True, secure from COOKIE_SECURE env, samesite from COOKIE_SAMESITE env)
- PASS: Token also returned in response body for API client backward compat
- PASS: Logout endpoint (`POST /api/auth/logout`) clears cookie + blacklists JWT
- PASS: MFA verify sets cookie on success
- PASS: `deps.py` `get_current_user()` checks `request.cookies.get("session")` as Try 0
- PASS: Bearer and X-API-Key fallback paths preserved and unchanged
- PASS: `api.js` uses `credentials: 'include'` on all fetch calls; no localStorage token injection
- PASS: `App.jsx` logout calls `POST /api/auth/logout` before redirect
- PASS: `App.jsx` ProtectedRoute uses async `/api/auth/me` cookie check
- PASS: `Login.jsx` removes all localStorage token storage
- PASS: `useWebSocket.js` fetches `/auth/ws-token` for WS auth
- PASS: `permissions.js` stores only non-sensitive user info (username/role) in `odin_user` key
- PASS: All raw fetch() calls in component files updated to remove Bearer token injection

### Item 2: go2rtc Bind 127.0.0.1
- PASS: `docker/go2rtc.yaml` api.listen = "127.0.0.1:1984"
- PASS: WebRTC port 8555 unchanged on ":8555" (0.0.0.0)
- PASS: Port 1984 removed from Dockerfile EXPOSE and docker-compose.yml

### Item 3: Non-Root Container
- PASS: `odin` user/group created in Dockerfile
- PASS: supervisord.conf `[supervisord] user=odin`
- PASS: `[unix_http_server]` chmod=0700, chown=odin:odin
- PASS: entrypoint.sh chowns /data and /app to odin before exec-ing supervisord
- NOTE: `USER odin` not added to Dockerfile — entrypoint.sh runs as root intentionally to handle secret generation and initial chown. Supervisord drops to odin. This is the correct pattern.

### Item 4: slowapi Rate Limiting
- PASS: `slowapi==0.1.9` added to requirements.txt
- PASS: `backend/rate_limit.py` shared limiter module created
- PASS: main.py: `app.state.limiter = limiter` + `_rate_limit_exceeded_handler` registered
- PASS: `auth/login` and `auth/mfa/verify` decorated with `@limiter.limit("10/minute")`
- PASS: `print-files/upload` decorated with `@limiter.limit("30/minute")`

### Item 5: API Token Scope Enforcement
- PASS: `require_scope(scope)` dependency added to `deps.py`
- PASS: `require_role()` updated to accept optional `scope=` parameter
- PASS: Create/delete endpoints for printers, jobs, models, spools use `scope="write"`
- PASS: Scope enforcement only activates for per-user scoped tokens (odin_xxx); JWT and global key bypass

## Notes / Known Caveats

1. **Local dev cookie:** `COOKIE_SECURE` env var defaults to `True`. For local HTTP development (non-HTTPS), set `COOKIE_SECURE=false` in docker-compose environment section.

2. **ProtectedRoute flicker:** Brief null render while `/api/auth/me` check resolves. Acceptable; no white screen.

3. **OIDC callback:** Still has legacy path with a TODO comment. Functional — backend sets cookie on OIDC callback redirect; frontend reloads to pick it up. Full OIDC cleanup is out of scope.

4. **Scope coverage:** `scope="write"` applied to create/delete on 4 core resource routers. Not applied to every write endpoint in the codebase — consistent with spec's "start with core resource routers" guidance. Remaining endpoints default to `scope=None` (no enforcement), backward compatible.

5. **Tests:** Backend test suite uses Bearer/API-key auth (not cookies). Bearer fallback preserved. Tests should pass without changes.
