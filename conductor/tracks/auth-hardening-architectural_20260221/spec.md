# Spec: Auth Hardening — Architectural Items

## Goal

Implement the 5 architectural security improvements identified during the pre-launch audit. These harden ODIN against XSS-based session theft, direct port exposure, container privilege escalation, brute-force amplification, and token over-permissioning.

## Context

v1.3.57–58 shipped endpoint-level hardening. This track addresses the session storage model, network exposure, container runtime privileges, rate limiting, and API token scoping — all changes that required design decisions before implementation.

---

## Item 1: httpOnly Cookie Auth (Replace localStorage JWT)

### Problem
JWT stored in `localStorage` is readable by any JavaScript — a single XSS vulnerability means session hijacking.

### Solution
- `login` endpoint returns `Set-Cookie: session=<JWT>; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=86400`
- Backend auth middleware checks `request.cookies.get("session")` first, then falls back to `Authorization: Bearer` header (for API clients), then `X-API-Key`
- `logout` endpoint clears the cookie via `Set-Cookie: session=; Max-Age=0`
- Frontend `api.js`: remove all `localStorage.getItem/setItem('token')` calls; remove `Authorization: Bearer` header injection; rely on browser cookie auto-send
- Frontend must set `credentials: 'include'` on all `fetch()` calls (already needed for CORS with cookies)
- CSRF: `SameSite=Strict` provides primary protection. No additional CSRF token needed for same-origin React SPA.

### What does NOT change
- `X-API-Key` header auth (perimeter auth, unaffected)
- Per-user API token auth via `X-API-Key` header (unaffected — API clients don't use cookies)
- Bearer token auth path (kept as fallback for API clients that already have tokens)
- OIDC/SSO flow: after OIDC callback, set cookie instead of returning token in redirect fragment

### Key files
- `backend/routers/auth.py` — `login`, `logout`, `oidc_callback`
- `backend/main.py` — auth middleware cookie check
- `frontend/src/api.js` — remove localStorage, add `credentials: 'include'`
- `frontend/src/App.jsx` — remove token state from localStorage init
- `frontend/src/pages/Login.jsx` — remove token storage on login response

---

## Item 2: go2rtc — Bind HLS/API to 127.0.0.1

### Problem
go2rtc binds its API and HLS server on `0.0.0.0:1984`, exposing it directly to the network. External clients can bypass ODIN's auth and access streams directly.

### Solution
- In `docker/go2rtc.yaml`: add `api: listen: "127.0.0.1:1984"` under the `api` section
- WebRTC data channel (port 8555) must remain on `0.0.0.0` for ICE candidates
- FastAPI already proxies go2rtc through `/api/v1/cameras` endpoints — internal-only binding is transparent to users
- Verify go2rtc YAML syntax: `api.listen` key

### Key files
- `docker/go2rtc.yaml`
- `docker-compose.yml` — port 1984 mapping can be removed if it was exposed for external access

---

## Item 3: Container Non-Root User

### Problem
All 7 supervised processes run as root inside the container. A container escape or process exploit has full root access.

### Solution
- Add to `Dockerfile` (or `docker/Dockerfile`):
  ```dockerfile
  RUN groupadd -r odin && useradd -r -g odin -d /app -s /sbin/nologin odin
  RUN chown -R odin:odin /app /data 2>/dev/null || true
  USER odin
  ```
- `/data` is a Docker volume — ownership must be set in `entrypoint.sh` or via an init container pattern
- supervisord runs as non-root: add `user=odin` to `[supervisord]` section in `supervisord.conf`
- Ensure log directories and socket files are writable by `odin` user
- Test: `docker exec odin-container whoami` should return `odin`

### Key files
- `Dockerfile` (find actual location: `docker/Dockerfile` or root `Dockerfile`)
- `docker/supervisord.conf`
- `docker/entrypoint.sh` — may need `chown` before dropping privileges

---

## Item 4: Global API Rate Limiting (slowapi)

### Problem
No global rate limiting on API endpoints. Brute-force and enumeration attacks are limited only by the login-attempt rate limiter already in place.

### Solution
- Add `slowapi==0.1.9` to `backend/requirements.txt`
- Configure limiter in `backend/deps.py` or `backend/main.py`:
  ```python
  from slowapi import Limiter, _rate_limit_exceeded_handler
  from slowapi.util import get_remote_address
  from slowapi.errors import RateLimitExceeded
  limiter = Limiter(key_func=get_remote_address)
  app.state.limiter = limiter
  app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
  ```
- Apply limits:
  - Auth endpoints (`/auth/login`, `/auth/register`, `/setup`): `@limiter.limit("10/minute")`
  - General API: `@limiter.limit("300/minute")` on the app level or per-router
  - File upload endpoints (`/models/upload`, `/revisions`): `@limiter.limit("30/minute")`
- Key by IP; trust `X-Forwarded-For` only if behind a known proxy

### Key files
- `backend/requirements.txt`
- `backend/main.py` or `backend/deps.py` — limiter setup
- `backend/routers/auth.py` — auth endpoint decorators
- `backend/routers/models.py` — upload endpoint decorators

---

## Item 5: API Token Scope Enforcement

### Problem
Per-user API tokens have a `scopes` field but it is not enforced at endpoints — any valid token grants full access.

### Solution
- Audit `api_tokens` table schema: confirm `scopes` column exists and its format (JSON array of strings)
- Define scope constants: `["read", "write", "admin"]` or more granular per-resource
- In the token verification path (wherever `X-API-Key` is matched to `api_tokens` table), extract scopes
- Create `require_scope(scope: str)` dependency in `deps.py`:
  ```python
  def require_scope(scope: str):
      def _check(current_user: dict = Depends(get_current_user)):
          token_scopes = current_user.get("token_scopes", [])
          if token_scopes and scope not in token_scopes:
              raise HTTPException(403, "Insufficient token scope")
      return _check
  ```
- Apply to write/delete endpoints: `Depends(require_scope("write"))`
- Read endpoints: `Depends(require_scope("read"))`
- Token creation UI/API: allow specifying scopes on creation

### Key files
- `backend/deps.py` — token verification + scope extraction
- `backend/routers/auth.py` — token CRUD + scope in create response
- `backend/schemas.py` — `APITokenCreate` schema: `scopes: list[str] = ["read", "write"]`

---

## Acceptance Criteria

- [ ] Login sets `session` cookie (HttpOnly, Secure, SameSite=Strict); no token in response body
- [ ] Logout clears cookie (Max-Age=0)
- [ ] Frontend makes API calls without Authorization header; uses `credentials: 'include'`
- [ ] API clients using Bearer token or X-API-Key still work (backward compat)
- [ ] OIDC callback sets cookie instead of returning token in redirect
- [ ] go2rtc API/HLS listens on 127.0.0.1:1984 only
- [ ] go2rtc WebRTC port 8555 remains on 0.0.0.0
- [ ] `docker exec container whoami` returns non-root user
- [ ] `/data` volume is writable by the container user
- [ ] Auth endpoints rate-limited to 10/minute per IP
- [ ] General API rate-limited to 300/minute per IP
- [ ] API tokens with `scopes: ["read"]` cannot call write/delete endpoints
- [ ] Existing tests pass (839+)

## Out of Scope

- Refresh token rotation (separate session management track)
- Per-endpoint scope granularity beyond read/write/admin
- go2rtc authentication (separate — go2rtc has its own auth config)

## Technical Notes

- CORS: with `credentials: 'include'`, CORS must have explicit origin (not `*`) — already the case in `main.py`
- Local dev: cookies require either HTTPS or `localhost`; Vite dev server proxies to localhost so this should work
- `SameSite=Strict` on localhost dev: may need `SameSite=Lax` in dev mode — use an env var `COOKIE_SAMESITE=Strict` defaulting to `Strict` in prod
- supervisord non-root: some process types (go2rtc binding to ports < 1024) may need `CAP_NET_BIND_SERVICE` — but we're using 1984/8555 which are > 1024, so no capabilities needed
