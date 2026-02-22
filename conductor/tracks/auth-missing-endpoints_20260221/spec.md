# Spec: Auth — Missing Authentication on Endpoint Groups

## Goal

Add authentication to all endpoints identified in the authorization security audit as missing `require_role` or `get_current_user` dependencies. These are systematic gaps across entire feature areas, not isolated misses.

## Endpoints Requiring Auth (add `require_role("viewer")` minimum unless noted)

### Analytics (`backend/routers/analytics.py`)
- `GET /stats` — full fleet operational data
- `GET /analytics` — job counts, printer utilization
- `GET /analytics/failures` — failure history, HMS codes
- `GET /analytics/time-accuracy` — print time accuracy data
- `GET /education/usage-report` — add org-scope filter (operator-gated, but currently returns ALL users' emails across orgs; add `WHERE group_id = :gid` filter for non-admin callers)

### Jobs (`backend/routers/jobs.py`)
- `GET /jobs` — currently uses `get_current_user` (returns None if no auth); change to `require_role("viewer")`
- `POST /jobs` — same issue
- `GET /jobs/{job_id}` — org check currently skipped when `current_user` is None; change to `require_role("viewer")`
- `GET /print-jobs` — no auth dependency at all
- `GET /print-jobs/stats` — no auth dependency at all
- `GET /print-jobs/unlinked` — no auth dependency at all
- `GET /failure-reasons` — no auth dependency
- `GET /presets` — no auth dependency
- `POST /jobs/{id}/approve` and `POST /jobs/{id}/reject` — use manual role check after feature gate; replace with `require_role("operator")`
- `POST /jobs/bulk-update` — add org-scope check per job ID (fetch each job's org, call `check_org_access` before mutating)
- `POST /jobs/{id}/repeat` — add `check_org_access(current_user, original.charged_to_org_id)` before creating clone
- `POST /jobs/{id}/link-print` — add org-access check on both job and print_job

### System (`backend/routers/system.py`)
- `POST /setup/network` — CRITICAL: no auth, no setup-lock check; add `require_role("admin")` AND `_setup_is_locked(db)` check
- `GET /search` (global search) — no auth; add `require_role("viewer")`
- `GET /maintenance/tasks` — no auth; add `require_role("viewer")`
- `GET /maintenance/logs` — no auth; add `require_role("viewer")`
- `GET /maintenance/status` — no auth; add `require_role("viewer")`
- `DELETE /maintenance/tasks/{id}` — currently `require_role("operator")`; change to `require_role("admin")`
- `DELETE /maintenance/logs/{id}` — currently `require_role("operator")`; change to `require_role("admin")`
- `GET /config` — no auth; add `require_role("admin")`
- `GET /spoolman/test` — no auth; add `require_role("admin")`
- `GET /hms-codes/{code}` — no auth; add `require_role("viewer")`
- Remove `"/metrics"` from the unauthenticated middleware bypass in `main.py` and add `require_role("viewer")` (or gate by API key)

### Cameras (`backend/routers/cameras.py`)
- `GET /cameras` — no auth dependency; add `require_role("viewer")`

### Auth/Groups (`backend/routers/auth.py`)
- `GET /groups/{group_id}` — IDOR: any operator sees any group; add check `if current_user["role"] != "admin" and current_user.get("group_id") != group_id: raise HTTPException(403)`
- `GET /groups` — no org filter; add `WHERE g.id = :user_group_id` for non-admin callers
- OIDC `default_role` default: change from `"operator"` to `"viewer"` at line ~929; change `auto_create_users` default to `False`

### License server hardcoded IP (`backend/routers/system.py`)
- Remove `"http://192.168.70.6:5000"` default for `LICENSE_SERVER_URL`; require explicit env var or fail with clear error message; change to `https://` if a real server URL is provided

## Acceptance Criteria

- [ ] All listed endpoints have authentication
- [ ] `POST /setup/network` has admin auth + setup-lock check
- [ ] Bulk job mutations check org access per job ID
- [ ] `GET /groups/{id}` non-admin callers can only see their own group
- [ ] `GET /education/usage-report` org-scoped for non-admin
- [ ] OIDC default role changed to "viewer", auto_create_users defaults to False
- [ ] Hardcoded license server IP removed
- [ ] `make test` passes (839+)

## Technical Notes

- Pattern for require_role: `current_user: dict = Depends(require_role("viewer"))`
- For `GET /jobs` and `POST /jobs` — currently `Depends(get_current_user)` which can return None; just change to `require_role("viewer")`
- `check_org_access` is in `deps.py` — import and use for bulk/repeat/link-print
- `/metrics` middleware bypass is in `main.py` lines ~305-316; remove `"/metrics"` from the path list and add auth to the metrics endpoint handler
- The `POST /setup/network` setup-lock pattern: look at `/setup/printer` for the existing pattern (`if _setup_is_locked(db): raise HTTPException(403, "Setup already completed")`)
