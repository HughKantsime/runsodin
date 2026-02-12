# O.D.I.N. Feature Roadmap

Updated 2026-02-12 at v1.3.4.

## Shipped in v1.2.0

- ~~Fleet Failure Analytics Dashboard~~
- ~~Printer Tags/Groups for Fleet Organization~~
- ~~Audit Log Viewer Page~~
- ~~PWA Manifest for Mobile Install~~
- ~~Estimated vs Actual Print Time Tracking~~
- ~~Filament Drying Log~~
- ~~Print Profiles / Presets~~
- ~~Timelapse Generation from Camera Streams~~

## Shipped in v1.3.x

- ~~MFA / Two-Factor Authentication~~
- ~~Scoped API Tokens (Per-User)~~
- ~~Session Management & Token Revocation~~
- ~~Print Quotas~~
- ~~Organizations & Multi-Tenancy~~ *(basic — CRUD + member/printer assignment, no implicit resource scoping yet)*
- ~~Cost Chargebacks & Department Billing~~
- ~~IP Allowlisting~~
- ~~GDPR Data Export & Right to Erasure~~
- ~~File / Model Versioning~~ *(list + upload revisions, no revert or job-level revision reference yet)*
- ~~WCAG 2.1 Accessibility~~ *(initial pass — landmarks, ARIA labels, dialog roles, skip-to-content)*
- ~~Bulk Operations UI~~ *(Jobs multi-select with cancel/priority/reschedule/delete)*
- ~~Backup Restore via UI~~
- ~~Broadened Data Retention Policies~~
- ~~Scheduled Reports~~ *(CRUD definitions only — no automated execution yet)*

---

## Backlog — Deepen Existing Implementations

### 1. Organizations — Full Resource Scoping
**Effort: High** — Org CRUD and member/printer assignment exist. Resource isolation does not.

- `org_id` filtering on all list endpoints (printers, jobs, spools, models)
- Org members only see their org's resources
- Shared printers: flag to make a printer visible across orgs
- Org-level settings: default filament, notification preferences, branding
- Org switcher in header (superadmin)
- This is the largest structural change — touches nearly every query

---

### 2. Scheduled Report Execution
**Effort: Medium** — CRUD for report_schedules exists. No background runner.

- Background task checks `next_run_at` and generates reports
- Render as HTML email with inline charts (or PDF attachment)
- Send via existing email infrastructure
- Update `next_run_at` after each run

---

### 3. Model Versioning — Revert & Job Reference
**Effort: Low** — Revision list and upload exist. No revert or job-level tracking.

- `POST /api/models/{id}/revisions/{rev}/revert` — restore a previous revision as current
- Jobs reference `model_revision_id` so you know exactly which version was printed
- Revision selector on job scheduling form

---

### 4. Quiet Hours Digest Delivery
**Effort: Low** — Suppression and digest formatting exist. No automated send.

- Background task fires at end of quiet hours window
- Batches suppressed alerts into single digest notification
- Sends via configured channels (email, push, webhooks)

---

### 5. Bulk Operations — Printers & Spools
**Effort: Low** — Jobs bulk UI done. Printers/Spools bulk API exists but no UI.

- Checkbox column on Printers and Spools tables
- Bulk action toolbar: enable/disable printers, add tag, archive spools

---

## Backlog — Small Fixes

### 6. Fix Frontend Password Validation Mismatch
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
