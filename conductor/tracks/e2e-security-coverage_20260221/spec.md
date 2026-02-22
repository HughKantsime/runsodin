# Spec: E2E Test Coverage — Security Hardening (v1.3.57–59)

## Goal

Write and ship a new pytest test file covering all security features shipped in v1.3.57, v1.3.58, and v1.3.59. These features have no existing test coverage. All tests run against the live container (no mocking).

## Context

- v1.3.57: api_key stripped from responses, camera URL sanitization, auth on tags/live-status, SSRF blocklist on printer create, last-admin protection, path traversal fix, ZIP bomb protection, defusedxml, HSTS, error sanitization
- v1.3.58: JWT entropy, numeric field bounds, camera URL validation, webhook SSRF, audit logs (login + password change), GDPR export completeness
- v1.3.59: httpOnly cookie auth, go2rtc 127.0.0.1 binding, non-root container, slowapi rate limiting, API token scope enforcement

Existing tests live in `tests/test_e2e/` (Playwright browser) and `tests/` (pytest API). New tests go in a new file: `tests/test_security_features.py`.

## Test Infrastructure

- Mirror pattern from `tests/conftest.py` (uses `BASE_URL`, `API_KEY`, `ADMIN_USERNAME`, `ADMIN_PASSWORD` env vars)
- Use `requests` library directly (not Playwright — these are API-level tests)
- Each test uses a unique throwaway username where user creation is needed (to avoid cross-test state)
- Helper: `admin_session()` — returns a `requests.Session` with cookie auth (POST /auth/login, store cookie)
- Helper: `bearer_client(token)` — returns a `requests.Session` with `Authorization: Bearer <token>`

## Tests to Write

### Group 1: Cookie Auth (v1.3.59 Item 1)

**test_login_sets_httponly_cookie**
- POST /api/auth/login with valid credentials
- Assert response sets `session` cookie
- Assert cookie has `HttpOnly` flag
- Assert response body still contains `access_token` (backward compat)

**test_cookie_auth_grants_access**
- Login via POST /api/auth/login (get Set-Cookie)
- Use the session cookie (no Authorization header) to GET /api/users/me
- Assert 200

**test_logout_clears_cookie**
- Login to get cookie
- POST /api/auth/logout with cookie
- Assert response deletes/expires the `session` cookie (Max-Age=0 or expired date)
- Assert GET /api/users/me with cleared cookie returns 401

**test_bearer_token_still_works**
- Login via POST /api/auth/login, extract `access_token` from body
- GET /api/users/me with `Authorization: Bearer <token>` (no cookie)
- Assert 200 (backward compat preserved)

**test_api_key_still_works**
- GET /api/users/me with `X-API-Key: <key>` only
- Assert 200

**test_ws_token_endpoint**
- Login with cookie, POST /api/auth/ws-token
- Assert 200 and response contains a short-lived token

### Group 2: Rate Limiting (v1.3.59 Item 4)

**test_login_rate_limit**
- POST /api/auth/login with wrong password 11 times in quick succession from same IP
- Assert at least one response is 429
- Note: existing login-attempt limiter already exists; this verifies the slowapi layer

**test_upload_rate_limit** (optional/soft — may skip if too slow)
- 31 rapid POST /api/models/upload attempts (small invalid file)
- Assert at least one 429

### Group 3: API Token Scope Enforcement (v1.3.59 Item 5)

**test_read_only_token_blocked_on_write**
- Create a per-user API token with `scopes: ["read"]`
- Attempt POST /api/printers (or another write endpoint) using that token
- Assert 403

**test_write_token_allowed_on_write**
- Create a per-user API token with `scopes: ["read", "write"]`
- Attempt POST /api/printers using that token
- Assert not 403 (may be 400/422 on bad data, but not 403 scope error)

### Group 4: go2rtc Port Isolation (v1.3.59 Item 2)

**test_go2rtc_not_exposed_externally**
- Attempt `GET http://localhost:1984/api` directly (bypassing ODIN)
- Assert connection refused or non-200 (port should be bound to 127.0.0.1 only, so localhost still reachable but we verify the binding change is in place)
- Note: from inside Docker this may still be reachable via host network; test verifies at the host level

### Group 5: Non-Root Container (v1.3.59 Item 3)

**test_container_runs_as_non_root**
- `docker exec odin whoami` (or equivalent)
- Assert output is NOT "root"

### Group 6: SSRF Blocklist (v1.3.57/58)

**test_printer_create_rejects_localhost**
- POST /api/printers with `api_host: "localhost"` or `"127.0.0.1"`
- Assert 400

**test_printer_create_rejects_link_local**
- POST /api/printers with `api_host: "169.254.1.1"`
- Assert 400

**test_webhook_create_rejects_internal_url**
- POST /api/webhooks with `url: "http://127.0.0.1/evil"`
- Assert 400

### Group 7: Input Validation / Numeric Bounds (v1.3.58)

**test_slot_count_upper_bound**
- PATCH /api/printers/{id} with `slot_count: 9999`
- Assert 422

**test_priority_upper_bound**
- PATCH /api/jobs/{id} with `priority: 999`
- Assert 422

**test_quantity_upper_bound**
- POST /api/jobs with `quantity: 99999`
- Assert 422

### Group 8: Camera URL Validation (v1.3.58)

**test_camera_rejects_non_rtsp_scheme**
- POST /api/printers with `camera_url: "http://evil.com/stream"`
- Assert 400

**test_camera_rejects_localhost**
- POST /api/printers with `camera_url: "rtsp://127.0.0.1/stream"`
- Assert 400

### Group 9: API Key Not Leaked in Responses (v1.3.57)

**test_api_key_not_in_printer_response**
- GET /api/printers (or /api/printers/{id})
- Assert response JSON does not contain `"api_key"` field at top level or in any printer object

### Group 10: Last-Admin Protection (v1.3.57)

**test_cannot_delete_last_admin**
- Create a throwaway admin user
- Delete the throwaway admin (should succeed — leaves at least the original admin)
- Attempt to delete the original admin (should 400)
- Verify: if only 1 admin exists, DELETE /api/users/{admin_id} returns 400

### Group 11: GDPR Export Completeness (v1.3.58)

**test_gdpr_export_includes_tokens_and_quota**
- GET /api/users/{current_user_id}/export
- Assert response JSON keys include `api_tokens` and `quota_usage`
- Assert `api_tokens` entries do NOT contain raw token values (only metadata)

### Group 12: Audit Log Events (v1.3.58)

**test_audit_log_records_login**
- Login successfully
- GET /api/audit-logs (admin)
- Assert at least one entry with `action` containing "login"

## File Location

`tests/test_security_features.py`

## Acceptance Criteria

- [ ] All tests in `test_security_features.py` pass (or are explicitly skipped with reason)
- [ ] `make test` still passes at 839+ (no regressions)
- [ ] New file follows existing conftest.py patterns (env vars, throwaway users, cleanup)
- [ ] No hardcoded credentials in test file

## Technical Notes

- Use `requests.Session` to preserve cookies across requests within a test
- For rate limit tests: use `time.sleep(0)` — don't slow the suite; just fire 11 rapid POSTs
- Container name for docker exec: find via `docker ps --filter name=odin --format '{{.Names}}'`
- Audit log endpoint: likely `GET /api/audit-logs` — confirm in routers/auth.py or routers/system.py
- GDPR export endpoint: `GET /api/users/{id}/export` — confirm path in auth.py
