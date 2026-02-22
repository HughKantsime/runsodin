# Plan: auth-missing-endpoints_20260221

## Summary
Add authentication to all endpoints identified in the authorization security audit. Changes span analytics.py, jobs.py, system.py, cameras.py, auth.py, and main.py.

## Tasks

### 1. analytics.py
- [x] 1.1 `GET /stats` — add `require_role("viewer")`
- [x] 1.2 `GET /analytics` — add `require_role("viewer")`
- [x] 1.3 `GET /analytics/failures` — add `require_role("viewer")`
- [x] 1.4 `GET /analytics/time-accuracy` — add `require_role("viewer")`
- [x] 1.5 `GET /education/usage-report` — add org-scope filter for non-admin callers

### 2. jobs.py
- [x] 2.1 `GET /jobs` — change `get_current_user` to `require_role("viewer")`
- [x] 2.2 `POST /jobs` — change `get_current_user` to `require_role("viewer")`
- [x] 2.3 `GET /jobs/{job_id}` — change `get_current_user` to `require_role("viewer")`
- [x] 2.4 `GET /print-jobs` — add `require_role("viewer")`
- [x] 2.5 `GET /print-jobs/stats` — add `require_role("viewer")`
- [x] 2.6 `GET /print-jobs/unlinked` — add `require_role("viewer")`
- [x] 2.7 `GET /failure-reasons` — add `require_role("viewer")`
- [x] 2.8 `GET /presets` — add `require_role("viewer")`
- [x] 2.9 `POST /jobs/{id}/approve` — replace manual role check with `require_role("operator")`
- [x] 2.10 `POST /jobs/{id}/reject` — replace manual role check with `require_role("operator")`
- [x] 2.11 `POST /jobs/bulk-update` — add org-scope check per job ID
- [x] 2.12 `POST /jobs/{id}/repeat` — add `check_org_access` before creating clone
- [x] 2.13 `POST /jobs/{id}/link-print` — add org-access check on both job and print_job

### 3. system.py
- [x] 3.1 `POST /setup/network` — add `require_role("admin")` + `_setup_is_locked()` check
- [x] 3.2 `GET /search` — add `require_role("viewer")`
- [x] 3.3 `GET /maintenance/tasks` — add `require_role("viewer")`
- [x] 3.4 `GET /maintenance/logs` — add `require_role("viewer")`
- [x] 3.5 `GET /maintenance/status` — add `require_role("viewer")`
- [x] 3.6 `DELETE /maintenance/tasks/{id}` — change `operator` to `admin`
- [x] 3.7 `DELETE /maintenance/logs/{id}` — change `operator` to `admin`
- [x] 3.8 `GET /config` — add `require_role("admin")`
- [x] 3.9 `GET /spoolman/test` — add `require_role("admin")`
- [x] 3.10 `GET /hms-codes/{code}` — add `require_role("viewer")`
- [x] 3.11 Remove hardcoded license server IP default

### 4. cameras.py
- [x] 4.1 `GET /cameras` — add `require_role("viewer")`

### 5. auth.py
- [x] 5.1 `GET /groups/{group_id}` — add IDOR check for non-admin callers
- [x] 5.2 `GET /groups` — add org filter for non-admin callers
- [x] 5.3 OIDC `default_role` — change from `"operator"` to `"viewer"`
- [x] 5.4 OIDC `auto_create_users` — change default from `True` to `False`

### 6. main.py
- [x] 6.1 Remove `"/metrics"` from the unauthenticated bypass list
- [x] 6.2 Add `require_role("viewer")` dep to the metrics endpoint handler in system.py

## Acceptance Criteria
- All listed endpoints have authentication
- `make test` passes (839+)
- Version bumped to 1.3.61 on completion
