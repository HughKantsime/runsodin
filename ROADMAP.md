# O.D.I.N. Feature Roadmap

Updated — see CHANGELOG for current version.

## Shipped in v1.3.70

- ~~500+ HMS error codes~~ (expanded from 42), clear HMS errors from UI, skip objects mid-print, print speed adjustment (25%–200%)
- ~~Resizable printer cards~~ (S/M/L/XL with persistent preference)
- ~~OBS streaming overlay~~ (`/overlay/:printerId` — no-auth page for OBS browser source)
- ~~Live application log viewer~~ (9 sources, level filter, search, SSE streaming)
- ~~Support bundle generator~~ (privacy-filtered diagnostic ZIP download)
- ~~Email-based user onboarding~~ (admin creates user → welcome email with random password)
- ~~Self-service password reset~~ (forgot-password flow via SMTP with 1-hour token)
- ~~Print archive~~ (auto-capture on job completion, archive browser with filters/search/detail modal)
- ~~Marketing site: Compare page, Reviews page, updated Features, updated nav~~ (runsodin.com)

## Shipped in v1.2.0

- ~~Fleet Failure Analytics Dashboard~~
- ~~Printer Tags/Groups for Fleet Organization~~
- ~~Audit Log Viewer Page~~
- ~~PWA Manifest for Mobile Install~~
- ~~Estimated vs Actual Print Time Tracking~~
- ~~Filament Drying Log~~
- ~~Print Profiles / Presets~~
- ~~Timelapse Generation from Camera Streams~~

## Shipped in v1.3.65

- ~~Path traversal sweep~~ — `realpath()` boundary checks on timelapse serve/delete, vision training export, and model revision revert; 100 MB limit on revision and backup uploads; 500 MB limit on ONNX uploads; trigger scan on backup restore; `label_class` allowlist validation

## Shipped in v1.3.64

- ~~Frontend security hardening~~ — Three.js bundled from npm (no CDN), CSP connect-src wildcards removed, OIDC dead code replaced with working oidc_code exchange, OIDC errors URL-encoded, Google Fonts CDN removed from Branding page

## Shipped in v1.3.62

- ~~Credential encryption at rest~~ — SMTP password, MQTT republish password encrypted with Fernet; camera_url no longer persists plaintext RTSP credentials; user-supplied camera URLs with embedded credentials encrypted on write

## Shipped in v1.3.61

- ~~Auth coverage sweep~~ — all endpoints from authorization audit now require `require_role()` (analytics, jobs, system, cameras, auth); OIDC defaults hardened; license server IP hardcoded removed

## Shipped in v1.3.x

- ~~MFA / Two-Factor Authentication~~
- ~~Scoped API Tokens (Per-User)~~
- ~~Session Management & Token Revocation~~
- ~~Print Quotas~~
- ~~Organizations & Multi-Tenancy~~
- ~~Cost Chargebacks & Department Billing~~
- ~~IP Allowlisting~~
- ~~GDPR Data Export & Right to Erasure~~
- ~~File / Model Versioning~~
- ~~WCAG 2.1 Accessibility~~ *(initial pass — landmarks, ARIA labels, dialog roles, skip-to-content)*
- ~~Bulk Operations UI~~ *(Jobs multi-select with cancel/priority/reschedule/delete)*
- ~~Backup Restore via UI~~
- ~~Broadened Data Retention Policies~~
- ~~Scheduled Reports~~
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
- ~~Installation-bound license activation (install ID, online/offline activation, binding validation)~~
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
- ~~API versioning: /api/v1/ prefix with backwards-compatible /api/ mount~~
- ~~CSP + security headers middleware (report-only)~~
- ~~Alembic migration framework with initial schema (27 tables, SQLite batch mode)~~
- ~~Vision endpoints: raw sqlite3 → SQLAlchemy (VisionDetection, VisionSettings, VisionModel)~~
- ~~Bulk CSV user import (POST /api/v1/users/import)~~
- ~~Education mode toggle (hides commerce UI)~~
- ~~Dead code removal: adapters.py, printer_adapter.py, stale migrations, systemd services~~
- ~~Branding cleanup: "PrintFarm Scheduler" → "O.D.I.N." across 12 files~~
- ~~UI industrial polish: status dots, card borders, table striping, theme tokens~~
- ~~Marketing site: Features page, Install page, updated navigation~~
- ~~Implicit org scoping: get_org_scope/check_org_access helpers, list/detail endpoint filtering~~
- ~~Upgrade UX: UpgradeBanner, UpgradeModal, Settings tier card with limit detection~~
- ~~Compliance docs: FERPA, COPPA, VPAT 2.5 accessibility template~~
- ~~README rewrite: badges, features, comparison table, install guide~~
- ~~Cloudflare Pages deploy config for runsodin.com~~
- ~~True light mode: 150+ CSS overrides, all 15 brand variables, status color remapping, card shadows~~
- ~~BrandingContext specificity fix: inline styles no longer clobber html.light CSS overrides~~
- ~~Theme toggle reactivity via MutationObserver~~
- ~~Theme-aware Recharts: CSS variables for chart grid/axis/tooltip across Analytics, EnergyWidget, AmsEnvironmentChart, PrinterTelemetryChart~~
- ~~Phase 0 supervisor STARTING race condition fix (retry up to 15s before failing)~~
- ~~Legal/compliance audit remediation: self-hosted fonts (GDPR), CORS hardening, MFA encryption guard, periodic session cleanup, error sanitization, alt text fixes, cache versioning, referrer policy, GDPR data export/erase UI, license type display, third-party notices~~
- ~~LICENSE file version reference fix (v0.17.0 → v1.0.0)~~
- ~~Code smell remediation: fix spoolman_spool_id attribute, audit_logs table name, bulk add_tag JSON logic, encrypt plug_auth_token, deduplicate quota helpers, normalize datetime.now(timezone.utc) across 12 files, replace deprecated utcnow(), remove dead code (verify_api_key, init_db), StaticPool→NullPool, bare except→except Exception (25 sites)~~
- ~~Frontend API consolidation: all pages migrated to centralized fetchAPI (Settings, Spools, Printers, Analytics, Orders, App.jsx), removed 6 local API layers (~200 lines), fixed auth header bugs, deduplicated getShortName, replaced alert/confirm with toast/ConfirmModal, SW cache cleanup~~
- ~~Printer dispatch: FTPS+MQTT for Bambu, HTTP upload+start for Moonraker/Klipper and PrusaLink; .3mf files stored on upload; manual Dispatch button on scheduled jobs with bed-clear confirmation; AUTO_DISPATCH env var for auto-send on IDLE transition~~
- ~~Security hardening pass (v1.3.57): api_key stripped from API responses, camera URL credential sanitization, auth added to live-status/tags/camera endpoints, last-admin protection, SSRF blocklist, XXE prevention (defusedxml), ZIP bomb protection, path traversal fix on revision upload, HSTS header, error message sanitization~~
- ~~Security hardening pass (v1.3.58): JWT 256-bit entropy, numeric field bounds (slot_count/quantity/priority/units_per_bed), camera URL scheme+SSRF+metachar validation, webhook SSRF validation, audit logs on login+password-change, GDPR export completeness (api_tokens, quota_usage)~~
- ~~Security hardening pass (v1.3.59): httpOnly session cookie auth (XSS protection), go2rtc HLS/API bound to 127.0.0.1 (network isolation), container non-root user via supervisord user=odin, global rate limiting via slowapi (10/min auth, 30/min upload), API token scope enforcement (read/write/admin scopes)~~

---

## Backlog — Deepen Existing Implementations

### ~~1. Organizations — Remaining Gaps~~ ✅ Shipped
Org-level settings shipped in v1.3.45: `settings_json` column on groups, GET/PUT endpoints, default filament applied on job creation, org quiet hours, org webhook dispatch, branding overlay, OrgManager settings panel UI.

---

### ~~2. Scheduled Reports — Email Delivery~~ ✅ Shipped
SMTP email delivery with HTML body implemented in `report_runner.py`.

---

### ~~5. Bulk Operations — Printers & Spools~~ ✅ Shipped
Checkbox selection + bulk action toolbars on both Printers and Spools pages.

---

## ~~Backlog — Small Fixes~~ (all shipped)

### ~~6. Fix Frontend Password Validation Mismatch~~ ✅ Shipped
Setup.jsx now enforces 8-char + uppercase + lowercase + digit with live strength checklist.

---

### ~~3. Dispatch Compatibility Guardrails~~ ✅ Shipped
Metadata extraction from gcode/3mf at upload time; `print_files.bed_x_mm/y_mm/compatible_api_types`; `printers.bed_x_mm/y_mm` config; API type + bed size guards in `dispatch_job()`; Models page compatibility badges; job modal inline bed mismatch warning. Shipped v1.3.56.

---

## Parked (needs more design)
