# O.D.I.N. Feature Roadmap

Updated 2026-02-12 at v1.2.0.

## Shipped in v1.2.0

- ~~Fleet Failure Analytics Dashboard~~
- ~~Printer Tags/Groups for Fleet Organization~~
- ~~Audit Log Viewer Page~~
- ~~PWA Manifest for Mobile Install~~
- ~~Estimated vs Actual Print Time Tracking~~
- ~~Filament Drying Log~~
- ~~Print Profiles / Presets~~
- ~~Timelapse Generation from Camera Streams~~

---

## Backlog — Enterprise & Education Readiness

### 1. MFA / Two-Factor Authentication
**Effort: Medium** — No implementation exists. Procurement blocker for enterprise and education IT.

TOTP-based 2FA (Google Authenticator, Authy, etc.):
- `user_totp_secret` column (encrypted at rest via existing Fernet key)
- `POST /api/auth/mfa/setup` — generate secret + QR code
- `POST /api/auth/mfa/verify` — validate TOTP code during login
- `DELETE /api/auth/mfa` — disable MFA (require current TOTP or admin override)
- Login flow: username/password → if MFA enabled → prompt for TOTP → issue JWT
- Admin can enforce MFA for all users via `/api/config/require-mfa`

Frontend: MFA setup in user profile, TOTP input step in login page.

---

### 2. Scoped API Tokens (Per-User)
**Effort: Medium** — Currently a single global `API_KEY`. Enterprise needs per-user tokens with granular permissions.

- `api_tokens` table: `id`, `user_id`, `name`, `token_hash`, `scopes` (JSON array), `expires_at`, `last_used_at`, `created_at`
- Scopes: `read:printers`, `write:printers`, `read:jobs`, `write:jobs`, `read:spools`, `admin`, etc.
- `POST /api/tokens` — create token (returns plaintext once), `GET /api/tokens` — list user's tokens, `DELETE /api/tokens/{id}` — revoke
- Middleware: check `X-API-Key` against `api_tokens` table, enforce scopes per route
- Keep legacy global API key for backward compatibility, deprecate over time

Frontend: token management page in user settings — create, list, copy, revoke.

---

### 3. Session Management & Token Revocation
**Effort: Low** — JWT has no revocation mechanism. Users can't see or kill active sessions.

- `active_sessions` table: `id`, `user_id`, `token_jti`, `ip_address`, `user_agent`, `created_at`, `last_seen_at`
- Record session on login, update `last_seen_at` on each authenticated request
- `GET /api/sessions` — list current user's sessions
- `DELETE /api/sessions/{id}` — revoke session (add `token_jti` to blacklist)
- `DELETE /api/sessions` — revoke all sessions except current
- Token blacklist checked in auth middleware (short-lived set, cleaned up after JWT expiry)
- Admin: `GET /api/admin/sessions` — view all active sessions, force-logout users

Frontend: active sessions list in user profile with device info, "Sign out everywhere" button.

---

### 4. Print Quotas
**Effort: Medium** — Feature flag exists in license system (`print_quotas`) but zero backend implementation. Critical for education.

- Quota config per user or per group: `quota_grams`, `quota_hours`, `quota_jobs`, `quota_period` (daily/weekly/monthly/semester)
- `quota_usage` table tracking consumption per period
- Enforcement at job creation: reject or warn when quota exceeded
- `GET /api/quotas` — current user's quota and usage
- `GET /api/admin/quotas` — admin view of all users' quota status
- `PUT /api/admin/quotas/{user_id}` — set user quotas
- `PUT /api/admin/quotas/group/{group_id}` — set group-level quotas (inherited by members)
- Quota reset on period boundary (cron or lazy evaluation)

Frontend: quota usage bar in sidebar/header, quota management in admin settings, "quota exceeded" state on job form.

---

### 5. Organizations & Multi-Tenancy
**Effort: High** — Groups exist (single-level team grouping) but no resource isolation. Enterprise with multiple departments needs scoped views.

Extend existing `groups` into full organizations:
- `org_id` foreign key on printers, jobs, spools, models — scoping all resources
- Org-level admins vs instance-level superadmin
- Org members only see their org's printers, jobs, spools, models
- Shared printers: flag to make a printer visible across orgs
- Org-level settings: default filament, notification preferences, branding
- `GET /api/orgs` — list orgs (superadmin), `POST /api/orgs`, `PATCH /api/orgs/{id}`
- All existing list endpoints gain implicit org filtering via auth context

Frontend: org switcher in header (superadmin), org management page, org assignment on printers.

This is the largest structural change — touches nearly every query. Should be implemented before scoped API tokens so tokens can inherit org context.

---

### 6. Cost Chargebacks & Department Billing
**Effort: Medium** — Cost calculation exists (`calculate_job_cost()`), but costs are never allocated to users/departments.

- `charged_to_user_id` and `charged_to_org_id` on jobs (auto-set from submitter)
- `GET /api/reports/chargebacks` — cost summary by user, org, date range (filterable)
- CSV/PDF export of chargeback reports for accounting
- Optional: `cost_center` field on orgs for ERP integration
- Dashboard widget: "This month's spend" per user/org

Frontend: cost column on job tables, chargeback report page (admin), personal spend summary in user profile.

---

### 7. IP Allowlisting
**Effort: Low** — No implementation. Enterprise networks want to restrict access.

- `ip_allowlist` config: list of CIDR ranges or individual IPs
- Middleware check on all routes (or configurable: API-only, UI+API)
- `GET /api/config/ip-allowlist`, `PUT /api/config/ip-allowlist` (superadmin only)
- Bypass for localhost/Docker internal networks
- Lock-out protection: always allow the IP that set the allowlist

Frontend: IP allowlist editor in admin settings with CIDR validation.

---

### 8. GDPR Data Export & Right to Erasure
**Effort: Low** — Zero implementation. Legal requirement in EU.

- `GET /api/users/{id}/export` — JSON dump of all user data: profile, jobs, audit log entries, sessions, alert preferences, quota usage
- `DELETE /api/users/{id}/erase` — anonymize user data: replace PII with "[deleted]", retain job records for analytics but strip user association
- Admin-only endpoints with audit log entry
- Include data inventory documentation (which tables store PII)

Frontend: "Export My Data" and "Delete My Account" buttons in user profile.

---

### 9. File / Model Versioning
**Effort: Medium** — No revision tracking. Operators lose track of which iteration of a model was printed.

- `model_revisions` table: `id`, `model_id`, `revision_number`, `file_path`, `changelog`, `uploaded_by`, `created_at`
- Uploading a new file to an existing model creates a revision, preserves old files
- `GET /api/models/{id}/revisions` — revision history
- `POST /api/models/{id}/revisions` — upload new revision
- `GET /api/models/{id}/revisions/{rev}/download` — download specific revision
- Jobs reference `model_revision_id` so you know exactly which version was printed

Frontend: revision history on model detail page, revision selector on job form, "Upload New Version" button.

---

### 10. WCAG 2.1 Accessibility
**Effort: Medium** — No ARIA attributes, no screen reader support, no semantic roles in current frontend.

Systematic pass across all frontend components:
- Add `aria-label`, `aria-describedby`, `aria-live` to interactive elements
- Add `role` attributes to custom widgets (modals, dropdowns, tabs)
- Add `sr-only` text for icon-only buttons
- Ensure all form inputs have associated `<label>` with `htmlFor`
- Keyboard navigation: all interactive elements focusable and operable via keyboard
- Color contrast: verify all text meets WCAG AA (4.5:1 ratio)
- Focus management: trap focus in modals, restore focus on close
- Skip-to-content link
- Required for Section 508 (US education) and EN 301 549 (EU)

No backend changes. Frontend-only effort, but touches every component.

---

## Backlog — Complete Partial Implementations

### 11. Bulk Operations UI
**Effort: Low** — Backend `POST /api/jobs/bulk` exists. No multi-select UI.

- Checkbox column on Jobs, Printers, Spools tables
- Select all / deselect all
- Bulk action toolbar: cancel jobs, change priority, reassign printer, delete, add tag (printers)
- Backend: `POST /api/jobs/bulk-update`, `POST /api/printers/bulk-update` for batch status/field changes

Frontend: multi-select state management, floating action bar on selection.

---

### 12. Backup Restore via UI
**Effort: Low** — Backup create/download/delete works. No restore.

- `POST /api/backups/restore` — upload a backup file, validate it, replace current DB
- Pre-restore validation: check SQLite integrity, schema version compatibility
- Auto-backup current DB before restore
- Restart advisory after restore (supervisor restart or container restart)

Frontend: "Restore" button on backup list, file upload dialog, confirmation with warnings.

---

### 13. Broadened Data Retention Policies
**Effort: Low** — Only vision frames have configurable retention. Jobs, audit logs, timelapses, alerts have none.

- Configurable retention per data type in admin settings:
  - Completed jobs: N days (default: unlimited)
  - Audit logs: N days (default: 365)
  - Timelapses: N days (default: 30)
  - Alert history: N days (default: 90)
  - Vision detections: already exists (7-90 days)
- Background cleanup task (daily cron via existing scheduler)
- `GET /api/config/retention`, `PUT /api/config/retention`

Frontend: retention settings panel in admin settings alongside existing vision retention slider.

---

### 14. Scheduled Reports
**Effort: Medium** — Quiet-hours digest exists. No periodic summaries.

- `report_schedules` table: `id`, `name`, `report_type`, `frequency` (daily/weekly/monthly), `recipients` (email list), `filters` (JSON), `next_run_at`
- Report types: fleet utilization, job summary, filament consumption, failure analysis, chargeback summary
- Render as HTML email with inline charts (or PDF attachment)
- `POST /api/report-schedules`, `GET /api/report-schedules`, `DELETE /api/report-schedules/{id}`
- Background runner checks `next_run_at` and sends via existing email infrastructure

Frontend: report schedule builder in admin settings — pick type, frequency, recipients, filters.

---

### 15. Fix Frontend Password Validation Mismatch
**Effort: Trivial** — Backend enforces 8-char + uppercase + lowercase + digit. `Setup.jsx` only checks 6-char minimum.

- Update `Setup.jsx` to match backend validation rules
- Single line change

---

## Parked (needs more design)

### Auto-Queue / Auto-Start Next Job
After print completes, automatically send next queued job to the printer. Requires file sending to printers, which requires guardrails:
- Extract printer profile from uploaded 3MF/gcode metadata
- Tag files with compatible printer type(s)
- Enforce compatibility at schedule time
- Final bed-size validation before send

Bambu (3MF over MQTT) is inherently safe — printer rejects incompatible files. Klipper/PrusaLink (raw gcode) is the risky case — needs metadata extraction + validation.
