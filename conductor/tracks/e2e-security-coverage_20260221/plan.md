# Plan: E2E Test Coverage — Security Hardening v1.3.57-59

## Summary

Write `tests/test_security_features.py` covering all security features shipped in
v1.3.57, v1.3.58, and v1.3.59. Run tests against live container. Fix failures. Then
verify no regressions with `make test`.

## DAG

```
1.0 Read patterns (DONE — conftest.py, test_security.py, auth.py, deps.py, schemas.py)
2.0 Write tests/test_security_features.py
3.0 Run pytest tests/test_security_features.py -v --tb=short
4.0 Fix any failures
5.0 Run make test — verify no regressions
6.0 Update metadata + commit
```

## Tasks

### Task 2.0 — Write test file
- [ ] 2.1 Write all test groups (cookie auth, rate limit, token scope, SSRF, input validation, GDPR, audit log)
- [ ] 2.2 Handle known API shape differences (webhook SSRF via /api/orgs/{id}/settings, not /api/webhooks)
- [ ] 2.3 Skip tests that cannot work in test environment with clear reasons

### Task 3.0 — Run tests
- [ ] 3.1 Run pytest tests/test_security_features.py -v --tb=short

### Task 4.0 — Fix failures
- [ ] 4.1 Fix any assertion errors or import issues

### Task 5.0 — Regression check
- [ ] 5.1 Run make test — verify 839+ tests pass

### Task 6.0 — Complete
- [ ] 6.1 Update metadata.json to COMPLETE
- [ ] 6.2 Commit changes

## Key API Findings

- Cookie auth: POST /api/auth/login sets `session` httpOnly cookie + returns access_token
- Logout: POST /api/auth/logout clears session cookie
- /api/auth/me: returns current user (NOT /api/users/me)
- /api/auth/ws-token: issues short-lived WebSocket token
- Rate limit: @limiter.limit("10/minute") on login route
- Scoped tokens: `odin_xxx` format, via X-API-Key header — scopes checked in require_role(scope="write")
- SSRF printer: _check_ssrf_blocklist on api_host in POST /api/printers
- SSRF webhook: _validate_webhook_url in PATCH /api/orgs/{id}/settings (not a standalone webhooks endpoint)
- Camera URL: _validate_camera_url blocks non-rtsp:// and localhost
- slot_count: Field(ge=1, le=16) on PrinterCreate, Field(ge=1, le=256) on PrinterUpdate
- priority: validator rejects int outside 0-10 (not 999 or larger)
- quantity: Field(ge=1, le=10000) on JobBase
- API key NOT in PrinterResponse: api_key is in PrinterBase (input) but PrinterResponse does not include api_key field
- GDPR export: GET /api/users/{id}/export — includes api_tokens (no token_hash/token_prefix) and quota_usage
- Audit logs: GET /api/audit-logs (admin only)
- Last admin: DELETE /api/users/{id} → 400 if deleting last admin
- Non-root: docker exec odin whoami
- go2rtc port: port 1984 is bound 127.0.0.1 inside container — accessible from test host
