# O.D.I.N. Feature Roadmap

Updated 2026-02-14 at v1.3.26.

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
- ~~Curl-pipe-bash installer with preflight checks~~
- ~~Self-updating updater with version diffing and rollback~~
- ~~Installer test suite (59 unit + integration tests)~~
- ~~Job matching improvements (time-window strategy, stale cleanup, deadlock/race fixes)~~
- ~~Auto-create jobs on print completion~~
- ~~Setup endpoint hardening (500 error fix)~~
- ~~Usage reports added to Pro tier~~
- ~~PRINTER_ERROR alert type~~
- ~~Rate limiter fix: only count failed logins toward IP limit~~
- ~~RBAC test expectations aligned with backend (printer create → operator, order update → admin)~~
- ~~Rate limit test isolation (throwaway usernames + backend restart between sessions)~~
- ~~Proactive stale schedule cleanup (fleet-wide, every scheduler run)~~
- ~~Settings/Audit frontend route RBAC guard~~
- ~~create_model field persistence fix (quantity_per_bed, units_per_bed, etc.)~~
- ~~Test suite: 13 failures resolved, test cheating eliminated (1022 passing, 0 xpassed)~~
- ~~Local dev/test/release pipeline (Makefile, deploy_local.sh, no sandbox required)~~
- ~~Cross-platform bump-version.sh (macOS BSD sed compatibility)~~
- ~~Setup endpoint locking after admin creation (SB-3 SSRF fix)~~
- ~~Login page 401 redirect loop fix~~
- ~~License feature gating: tier definition fallback for post-issuance features~~
- ~~Console error cleanup (dead /api/settings fetch, orgId type guard, alert enum mismatch)~~
- ~~Model versioning revert, job-level revision tracking, revision picker in job form~~
- ~~Bulk operations on Printers and Spools pages~~
- ~~Report runner daemon (scheduled report execution + quiet hours digest)~~
- ~~Org resource scoping (org_id filtering on list endpoints, shared printer flag, org switcher)~~
- ~~Hardcoded credentials removed, test env sanitized for public repo~~
- ~~Timeline click-to-inspect with detail drawer (scheduled job details + MQTT job details)~~
- ~~CameraModal live print context (info bar in large mode, full sidebar in fullscreen)~~
- ~~Shared PrinterPanels component (extracted from CameraDetail for reuse)~~
- ~~Toast notification system (react-hot-toast, replacing all alert/confirm calls)~~
- ~~React Error Boundary + 404 page~~
- ~~ConfirmModal + shared utilities (ONLINE_THRESHOLD_MS, getShortName, formatDuration)~~
- ~~Dashboard clickable printer cards with status badges, clickable stat cards, TV Mode button~~
- ~~Timeline touch support, auto-scroll to now, refetch interval~~
- ~~Models search, category count badges, schedule enhancements~~
- ~~Upload progress indicator~~
- ~~Jobs form improvements (quantity, printer in create modal), confirmation dialogs on all actions~~
- ~~Settings Access tab accordion, Pro tab visibility for Community, save scope clarity~~
- ~~Admin user search/filter, AuditLogs user column + search + readable details~~
- ~~Analytics date range selector, Utilization time range fix~~
- ~~Order cancellation, shipping modal, search, date column, line item editing~~
- ~~Spool search, action labels, DryingModal bug fix, error handling~~
- ~~Detection bulk review + inline actions~~
- ~~Camera retry/reconnect, snapshot, PiP button, Control Room CSS fix~~
- ~~Timelapse bulk delete~~
- ~~Modal accessibility (Escape, backdrop click) across ~15 modals~~
- ~~Login UX (OIDC loading, MFA auto-submit, SSO divider), Setup password strength checklist~~
- ~~Backend router split: main.py (11,550 lines) → 13 router modules + deps.py~~
- ~~JWT secret hardening (fail-fast on missing env var)~~
- ~~CORS hardening (explicit method/header lists)~~
- ~~GTM plan, Terms of Service, Privacy Policy, Vigil AI disclaimer~~

---

## Backlog — Deepen Existing Implementations

### 1. Organizations — Remaining Gaps
**Effort: Medium** — Resource scoping (org_id filtering, shared printers, org switcher) shipped in v1.3.19. Remaining:

- Org members only see their org's resources by default (implicit scoping, not just filtering)
- Org-level settings: default filament, notification preferences, branding

---

### 2. Scheduled Reports — Remaining Gaps
**Effort: Low** — Report runner daemon shipped in v1.3.19. Remaining:

- Render as HTML email with inline charts (or PDF attachment)
- Send via existing email infrastructure (currently generates but doesn't email)

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

---

## Parked (needs more design)

### Auto-Queue / Auto-Start Next Job
After print completes, automatically send next queued job to the printer. Requires file sending to printers, which requires guardrails:
- Extract printer profile from uploaded 3MF/gcode metadata
- Tag files with compatible printer type(s)
- Enforce compatibility at schedule time
- Final bed-size validation before send

Bambu (3MF over MQTT) is inherently safe — printer rejects incompatible files. Klipper/PrusaLink (raw gcode) is the risky case — needs metadata extraction + validation.
