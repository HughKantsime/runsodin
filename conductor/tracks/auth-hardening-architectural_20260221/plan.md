# Plan: Auth Hardening — Architectural Items
# Track: auth-hardening-architectural_20260221

## Overview

Implement 5 architectural security improvements across backend and frontend. Items are mostly independent and can be done in sequence. Item 1 (cookies) is the largest and touches the most files; the others are small, targeted changes.

## Dependency DAG

```
Item 2 (go2rtc bind)      → independent
Item 3 (non-root user)    → independent
Item 4 (slowapi)          → independent
Item 5 (scope enforcement) → reads api_tokens.scopes which already exists (confirmed TEXT DEFAULT '[]')
Item 1 (cookie auth)       → must update deps.py get_current_user, auth.py login/logout/mfa, frontend api.js, App.jsx, Login.jsx
```

Items 2, 3, 4, 5 can be done in any order. Item 1 last since it's the most invasive.

---

## Tasks

### Task 1: go2rtc — Bind HLS/API to 127.0.0.1
**File:** `docker/go2rtc.yaml`
**Change:** Replace `listen: ":1984"` with `listen: "127.0.0.1:1984"` in the `api` section.
**Also:** `Dockerfile` — remove `EXPOSE 1984` since the port is no longer externally exposed.
**Also:** `docker-compose.yml` — remove `1984:1984` port mapping if present (check first).

- [ ] 1.1 Edit `docker/go2rtc.yaml`: change `api.listen` to `"127.0.0.1:1984"`
- [ ] 1.2 Edit `Dockerfile`: remove `EXPOSE 1984` from the EXPOSE line
- [ ] 1.3 Check `docker-compose.yml` for `1984:1984` port mapping and remove it if present

---

### Task 2: Container Non-Root User
**Files:** `Dockerfile`, `docker/supervisord.conf`, `docker/entrypoint.sh`

Key observations:
- Dockerfile is at the repo root (`/Dockerfile`), not `docker/Dockerfile`
- supervisord.conf has `[supervisord] user=root` — change to `odin`
- entrypoint.sh runs as root initially (sets up secrets, writes files to `/data`) — must keep chown in entrypoint before exec-ing supervisord

- [ ] 2.1 Edit `Dockerfile`: after `RUN mkdir -p /data ...`, add user creation and ownership:
  ```dockerfile
  RUN groupadd -r odin && useradd -r -g odin -d /app -s /sbin/nologin odin
  ```
  NOTE: Do NOT add `USER odin` here — entrypoint.sh runs as root to handle secrets and chown. Instead supervisord drops to odin via `user=odin`.
- [ ] 2.2 Edit `docker/supervisord.conf`: change `user=root` to `user=odin` in `[supervisord]` section
- [ ] 2.3 Edit `docker/entrypoint.sh`: add `chown -R odin:odin /app /data 2>/dev/null || true` before the final `exec supervisord ...` call. Also fix socket ownership: `mkdir -p /var/run && chown odin:odin /var/run 2>/dev/null || true`
- [ ] 2.4 Edit `docker/supervisord.conf`: update `[unix_http_server]` socket to have correct permissions — add `chmod=0700` and `chown=odin:odin`

---

### Task 3: Global Rate Limiting via slowapi
**Files:** `backend/requirements.txt`, `backend/main.py`, `backend/routers/auth.py`, `backend/routers/models.py`

Key observations:
- Login already has its own DB-based rate limiter (`_check_rate_limit`) — slowapi adds a complementary HTTP-level layer
- Need to add `Request` parameter to endpoints for slowapi decorator to work
- Auth router login endpoint already takes `Request` — good
- Models upload endpoint needs checking

- [ ] 3.1 Edit `backend/requirements.txt`: add `slowapi==0.1.9` after the existing rate limiting comment area
- [ ] 3.2 Edit `backend/main.py`: add slowapi setup after imports:
  ```python
  from slowapi import Limiter, _rate_limit_exceeded_handler
  from slowapi.util import get_remote_address
  from slowapi.errors import RateLimitExceeded

  limiter = Limiter(key_func=get_remote_address)
  ```
  Then after `app = FastAPI(...)`:
  ```python
  app.state.limiter = limiter
  app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
  ```
- [ ] 3.3 Edit `backend/routers/auth.py`: add `@limiter.limit("10/minute")` to `login` and `register` and `setup` endpoints. Import limiter from main or create a shared instance in deps.py. Use a module-level import approach: create `backend/rate_limit.py` with the limiter instance, import it in auth.py and main.py.
  - Create `backend/rate_limit.py` with the shared limiter instance
  - Import in `main.py` and remove duplicate limiter creation
  - Import in `auth.py` and add `@limiter.limit("10/minute")` to login
- [ ] 3.4 Check `backend/routers/models.py` for upload endpoint and add `@limiter.limit("30/minute")`

---

### Task 4: API Token Scope Enforcement
**Files:** `backend/deps.py`

Key observations:
- `api_tokens` table has `scopes TEXT DEFAULT '[]'` (confirmed in entrypoint.sh)
- `deps.py` `get_current_user()` already extracts scopes: `user_dict["_token_scopes"] = json.loads(candidate.scopes) if candidate.scopes else []`
- The `_token_scopes` key is already populated — just need a `require_scope()` dependency

- [ ] 4.1 Edit `backend/deps.py`: add `require_scope()` dependency function after `require_role()`:
  ```python
  def require_scope(scope: str):
      """Dependency that enforces API token scope for per-user token auth.

      Only enforced when the request uses a per-user API token (odin_xxx format).
      JWT and global API key auth bypass scope checks (they have full access).
      """
      async def _check(current_user: dict = Depends(get_current_user)):
          if not current_user:
              raise HTTPException(status_code=401, detail="Not authenticated")
          token_scopes = current_user.get("_token_scopes", [])
          # Only enforce if scopes are present (i.e., per-user token auth)
          if token_scopes and scope not in token_scopes:
              raise HTTPException(status_code=403, detail="Insufficient token scope")
          return current_user
      return _check
  ```
- [ ] 4.2 Audit write/delete endpoints in key routers and add `Depends(require_scope("write"))` where appropriate. Start with printers, jobs, models, spools — the core resource routers. Read endpoints get `require_scope("read")` but since JWT auth bypasses this, it only affects scoped token users. Focus on write/delete first.
  - `backend/routers/printers.py` — POST/PATCH/DELETE endpoints
  - `backend/routers/jobs.py` — POST/PATCH/DELETE endpoints
  - `backend/routers/models.py` — POST/PATCH/DELETE endpoints
  - `backend/routers/spools.py` — POST/PATCH/DELETE endpoints

---

### Task 5: httpOnly Cookie Auth
**Files:** `backend/routers/auth.py`, `backend/deps.py`, `frontend/src/api.js`, `frontend/src/App.jsx`, `frontend/src/pages/Login.jsx`

Key observations:
- `backend/routers/auth.py` login returns `{"access_token": ..., "token_type": "bearer"}` — needs to also set cookie
- No logout endpoint exists yet — must create it
- `backend/deps.py` `get_current_user()` uses `OAuth2PasswordBearer` (Bearer header) as primary — needs cookie fallback added first
- `frontend/src/api.js` `fetchAPI()` reads `localStorage.getItem("token")` and injects `Authorization: Bearer` — remove this, add `credentials: 'include'`
- `frontend/src/App.jsx`:
  - `isTokenValid()` decodes localStorage token — replace with `/api/auth/me` call or cookie-based check
  - `ProtectedRoute` calls `isTokenValid()` — needs updating
  - Logout button: `localStorage.removeItem("token")` + redirect — replace with `POST /api/auth/logout` + redirect
- `frontend/src/pages/Login.jsx`:
  - `completeLogin()` does `localStorage.setItem('token', ...)` — remove
  - OIDC callback reads `?token=` from URL and stores in localStorage — replace with `/api/auth/oidc-set-cookie` or rely on backend redirect setting cookie
- MFA: `mfa_verify` also returns a token — needs to set cookie too

Backend cookie spec:
```
Set-Cookie: session=<JWT>; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=86400
```
For local dev (non-HTTPS), omit `Secure` flag — controlled via `COOKIE_SECURE` env var (default true).

- [ ] 5.1 Edit `backend/routers/auth.py` — login endpoint: change return to use `Response` with `Set-Cookie`:
  - Import `Response` from fastapi (already imported)
  - Create response object, call `response.set_cookie(...)`, return `{"token_type": "bearer", "mfa_required": False}` (no token in body)
  - Keep MFA flow: when `mfa_required`, still return `{"access_token": mfa_token, "token_type": "bearer", "mfa_required": True}` in body (MFA token is short-lived, not a session)
- [ ] 5.2 Edit `backend/routers/auth.py` — add logout endpoint:
  ```python
  @router.post("/auth/logout", tags=["Auth"])
  async def logout(response: Response, current_user: dict = Depends(get_current_user)):
      response.delete_cookie("session", path="/")
      # Also blacklist the JWT if present
      return {"detail": "Logged out"}
  ```
- [ ] 5.3 Edit `backend/routers/auth.py` — mfa_verify: set cookie on full token issue (after successful TOTP)
- [ ] 5.4 Edit `backend/deps.py` — `get_current_user()`: add cookie check as Try 0 (before Bearer):
  ```python
  # Try 0: session cookie (httpOnly, browser-based auth)
  session_token = request.cookies.get("session")
  if session_token:
      # Same logic as Bearer token path
      ...
  ```
- [ ] 5.5 Edit `frontend/src/api.js` — `fetchAPI()`:
  - Remove `const token = localStorage.getItem("token")` and Bearer header injection
  - Add `credentials: 'include'` to the fetch call
  - Keep X-API-Key injection (for env-var-configured perimeter key)
- [ ] 5.6 Edit `frontend/src/App.jsx`:
  - Replace `isTokenValid()` (localStorage decode) with a cookie-aware version: call `/api/auth/me` or simply attempt navigation and handle 401 redirect
  - Simplest approach: replace localStorage check with a call to `GET /api/auth/me` — if 200, user is valid; if 401, redirect to login. Make this async in a `useEffect` on ProtectedRoute.
  - Update logout button: call `POST /api/auth/logout` then redirect to `/login`
  - Remove `localStorage.removeItem("token")` / `localStorage.removeItem("user")` calls
- [ ] 5.7 Edit `frontend/src/pages/Login.jsx`:
  - Remove `completeLogin()` function and all `localStorage.setItem('token', ...)` calls
  - After successful login, call `refreshPermissions()` and navigate to `/`
  - OIDC callback: remove `localStorage.setItem('token', urlToken)` — backend already sets cookie via redirect; just trigger page reload to `/`
- [ ] 5.8 Edit `backend/config.py` (if exists) or `backend/main.py`: add `COOKIE_SECURE` env var support — default `True`, can be `False` for local dev over HTTP
- [ ] 5.9 Edit `frontend/src/permissions.js`: check if `refreshPermissions()` uses localStorage token — if so, update to use cookie-based fetch (credentials: include)
- [ ] 5.10 Check `frontend/src/hooks/useWebSocket.js`: WebSocket auth uses `?token=` query param from localStorage — needs a path forward. Options: (a) keep localStorage only for WebSocket token (not ideal), (b) add a `/api/auth/ws-token` endpoint that issues a short-lived token for WebSocket auth, (c) cookies don't work on WebSocket upgrades so keep Bearer token in localStorage only for WS. Decision: keep a separate short-lived WS token. Implement `/api/auth/ws-token` endpoint that issues a 5-minute JWT, called from `useWebSocket.js` instead of reading localStorage.

---

## Acceptance Checklist

- [ ] `docker/go2rtc.yaml` `api.listen` = `"127.0.0.1:1984"`
- [ ] go2rtc WebRTC port 8555 remains on `0.0.0.0`
- [ ] Port 1984 removed from EXPOSE and docker-compose
- [ ] `odin` user created in Dockerfile
- [ ] supervisord runs as odin user
- [ ] `/data` writeable by odin via chown in entrypoint.sh
- [ ] `slowapi` in requirements.txt
- [ ] limiter configured in main.py with exception handler
- [ ] Auth endpoints decorated with `@limiter.limit("10/minute")`
- [ ] Upload endpoints decorated with `@limiter.limit("30/minute")`
- [ ] `require_scope()` exists in deps.py
- [ ] Write/delete endpoints in core routers use `require_scope("write")`
- [ ] Login sets httpOnly session cookie (no token in response body for normal flow)
- [ ] Logout endpoint clears cookie
- [ ] MFA verify sets cookie on success
- [ ] `get_current_user` checks cookie before Bearer header
- [ ] `frontend/src/api.js` uses `credentials: 'include'`, no localStorage token
- [ ] `App.jsx` logout calls `POST /api/auth/logout`
- [ ] `App.jsx` ProtectedRoute uses async `/api/auth/me` check
- [ ] `Login.jsx` removes all localStorage token storage
- [ ] `useWebSocket.js` uses WS-token endpoint instead of localStorage
- [ ] `make test` passes

---

## Implementation Order

1. Task 1 (go2rtc) — 2 file edits, zero risk
2. Task 2 (non-root) — Dockerfile + supervisord.conf + entrypoint.sh
3. Task 3 (slowapi) — requirements.txt + main.py + rate_limit.py + auth.py decorators
4. Task 4 (scope enforcement) — deps.py + 4 router files
5. Task 5 (cookies) — largest; backend auth changes + frontend overhaul

## Notes

- No test file changes needed for items 1–3 (infrastructure/config)
- Items 4–5 may affect RBAC tests if they rely on Bearer tokens — existing Bearer fallback must remain
- Version bump to 1.3.48 after all items complete
