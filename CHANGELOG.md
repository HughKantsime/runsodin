# Changelog

All notable changes to O.D.I.N. are documented here.

## [1.3.30] - 2026-02-16

### Fixed
- **Phase 0 supervisor race condition** — Supervisor services still in STARTING state caused false failures when Phase 0B ran immediately after Docker healthcheck passed; now retries up to 15s before failing

## [1.3.29] - 2026-02-15

### Added
- **True light mode** — Complete light theme with 150+ CSS overrides: all 15 `--brand-*` variables, farm palette opacity variants, status color remapping (red-50/green-50/blue-50 tints for light surfaces), scrollbars, focus rings, selection highlighting, table rows, skeleton loaders, card elevation shadows
- **Chart CSS variables** — `--chart-card-bg`, `--chart-grid`, `--chart-axis`, `--chart-tooltip-bg/border/shadow` for theme-aware Recharts rendering

### Fixed
- **BrandingContext specificity bug** — `applyBrandingCSS()` inline styles clobbered `html.light` CSS overrides; light mode now removes surface variable inline styles so the stylesheet wins
- **Theme toggle reactivity** — MutationObserver on `document.documentElement.classList` reapplies branding when user switches between light/dark mode
- **Analytics page dark-on-light** — Replaced hardcoded `rgba(17,24,39,0.8)` card backgrounds and `#111827`/`#1F2937`/`#374151` Recharts colors with CSS variable references across Analytics, EnergyWidget, AmsEnvironmentChart, and PrinterTelemetryChart
- **Invisible text in light mode** — `text-white` fallbacks on Analytics stat values changed to `text-farm-100` (resolves to near-black on light backgrounds)

## [1.3.28] - 2026-02-14

### Added
- **Implicit org scoping** — `get_org_scope()` and `check_org_access()` helpers; printers, jobs, spools, and models list/detail endpoints filtered by org_id (superadmins bypass, NULL org_id visible to all)
- **Upgrade UX** — `UpgradeBanner` (session-dismissible amber banner for Community tier), `UpgradeModal` (friendly limit-hit modal with pricing link), Settings tier info card with upgrade CTA
- **Compliance docs** — FERPA compliance statement, COPPA compliance for K-12, VPAT 2.5 accessibility template (WCAG 2.1 AA)
- **Cloudflare Pages deploy config** — SPA routing (`_redirects`), security headers (`_headers`), deployment guide for runsodin.com

### Changed
- **README rewrite** — Replaced minimal README with badges, feature highlights, comparison table, install instructions, and architecture overview
- **LicenseContext** — Now exposes `max_printers` and `max_users` for limit-hit detection in Add Printer/Add User flows

## [1.3.27] - 2026-02-14

### Added
- **API versioning** — All routes available at both `/api/` (backwards compat) and `/api/v1/` (versioned); Swagger docs at `/api/v1/docs`
- **CSP security headers** — Content-Security-Policy (report-only), X-Content-Type-Options, X-Frame-Options, Referrer-Policy, Permissions-Policy on all responses
- **Alembic migrations** — Framework with initial schema covering 27 SQLAlchemy tables; SQLite batch mode enabled
- **Bulk CSV user import** — `POST /api/v1/users/import` admin endpoint with validation, duplicate skipping, license limit enforcement
- **Education mode toggle** — Hides commerce UI (Orders, Products, Consumables) when enabled via Settings
- **Marketing site pages** — Features page (8 categories from FEATURES.md), Install/Getting Started page with copy-able commands; updated navigation

### Changed
- **Vision endpoints unified** — Converted all Vigil AI API endpoints from raw sqlite3 to SQLAlchemy (3 new models: VisionDetection, VisionSettings, VisionModel)
- **UI industrial polish** — Printer status dots replace large badges, cards get subtle borders, Jobs table alternating rows with monospace headers, theme token consistency
- **Branding cleanup** — All "PrintFarm Scheduler" references replaced with "O.D.I.N." across 12 files; VAPID email updated to runsodin.com

### Removed
- Dead code: `adapters.py`, `printer_adapter.py`, stale `migrations/`, `systemd/` services, `setup-dev.sh`, `GETTING_STARTED.md`
- Unused deps: `axios`, `@types/react`, `@types/react-dom`
- Debug noise: `print()` calls replaced with logger, WebSocket `console.log` removed

### Fixed
- Hardcoded Spoolman URL replaced with `settings.spoolman_url`
- Stale `(v0.17.0)` version comments removed

## [1.3.26] - 2026-02-14

### Changed
- **Backend router split** — Refactored monolithic `main.py` (11,550 lines, 306 routes) into 13 modular router files under `backend/routers/` with shared dependencies in `backend/deps.py`; `main.py` reduced to 294-line app shell
- **CORS hardening** — Scoped `allow_methods` and `allow_headers` from wildcards to explicit lists

### Fixed
- **JWT secret security** — Removed insecure hardcoded fallback secret in `auth.py`; now fails fast if `JWT_SECRET_KEY` env var is missing

### Added
- **GTM Plan** — Comprehensive go-to-market strategy document covering three-market approach (prosumer, education, defence/gov), pricing tiers, financial model, and execution phases
- **Legal documents** — Terms of Service (EULA for all 4 license tiers), Privacy Policy (GDPR-compliant, self-hosted focus), Vigil AI safety disclaimer

## [1.3.25] - 2026-02-13

### Added
- **Toast notification system** — react-hot-toast replaces ~30 `alert()`/`confirm()` calls with styled dark-theme toasts; all mutations now surface success/error feedback
- **React Error Boundary** — wraps Routes in App.jsx; uncaught render errors show a recovery UI instead of a white screen
- **ConfirmModal component** — shared confirmation dialog with focus management, Escape-to-cancel, backdrop click, and danger/primary variants; used across all destructive actions
- **Shared utilities** (`utils/shared.js`) — `ONLINE_THRESHOLD_MS`, `getShortName`, `formatDuration` extracted from duplicated code in 4+ files
- **404 page** — catch-all route with "Page not found" and link to Dashboard
- **Dashboard clickable printer cards** — cards navigate to Printers page, now show explicit status badge (Printing/Idle/Error/Offline), stat cards link to relevant pages
- **Dashboard TV Mode button** — quick-access link to `/tv` from Dashboard header
- **Timeline touch support** — `touchstart`/`touchmove`/`touchend` handlers for tablet drag-to-reschedule
- **Timeline auto-scroll** — viewport auto-centers on the Now indicator on mount
- **Models search** — text search input filtering by model name
- **Upload progress indicator** — XMLHttpRequest with `onprogress` for percentage bar during .3mf uploads
- **Jobs form improvements** — quantity field and optional printer dropdown added to CreateJobModal
- **Settings Access tab accordion** — 10 stacked components collapsed into 6 collapsible sections
- **Admin user search** — text search and role filter on user management table
- **AuditLogs User column** — who performed the action, plus text search and human-readable detail summaries
- **Analytics date range selector** — 7/14/30/90 day selector
- **Order cancellation** — cancel button for pending/in-progress orders with status transition
- **Order shipping modal** — proper form replacing `prompt()`, with tracking number input and carrier dropdown
- **Order search and date column** — text search for order#/customer, date column in table
- **Order line item editing** — line items editable in edit modal (previously locked after creation)
- **Spool search** — text search by brand/name/material
- **Spool action labels** — visible text labels on SpoolCard buttons (hidden below lg breakpoint)
- **Detection bulk review** — checkbox selection with bulk confirm/dismiss action bar
- **Detection inline actions** — confirm/dismiss buttons directly on pending detection cards
- **Camera retry/reconnect** — retry button on WebRTC error/disconnect with exponential backoff
- **Camera snapshot** — capture button on CameraDetail saves video frame as PNG
- **Camera PiP button** — picture-in-picture accessible from CameraCard hover overlay
- **Timelapse bulk delete** — checkbox selection with bulk delete
- **Login UX improvements** — OIDC loading indicator, MFA auto-submit on 6 digits, SSO divider, forgot-password guidance
- **Setup password strength** — real-time checklist showing which requirements are met as user types
- **ProGate back navigation** — back button for Community users hitting Pro-gated pages

### Fixed
- **DryingModal infinite fetch** (CRITICAL) — `useState` misused as `useEffect` caused re-fetch on every render
- **"Awaiting Approval" badge count** — tab badge showed total job count instead of approval-pending count
- **Setup password placeholder** — said "Min 6 characters" but validation required 8+ with uppercase/lowercase/number
- **Printer API key not sent** — Moonraker/PrusaLink API key field value was silently discarded on form submit
- **Products React key** — missing key on Fragment inside `.map()` causing reconciliation issues
- **CameraModal fullscreen close** — backdrop click in fullscreen mode no longer closes the modal accidentally
- **Silent error handling** — replaced empty catch blocks in Alerts, AlertBell, EmergencyStop, WebhookSettings with toast errors
- **Consumables permission keys** — documented intentional sharing of `models.*` permissions
- **Utilization stale headers** — module-scope API headers now computed per-request
- **Utilization time range** — selector now re-queries server instead of only slicing client-side data

### Changed
- All destructive actions (delete, cancel, disable) now require confirmation via ConfirmModal
- All mutations surface success/error feedback via toast notifications
- `fetchAPI` now parses error response body for `detail`/`message` before throwing
- `users` API namespace refactored from manual fetch to use `fetchAPI`
- Dashboard maintenance items and alert widgets link to their respective pages
- Dashboard AlertsWidget uses Lucide icons instead of emoji
- Timeline refetches every 30s; shows empty state when no jobs exist
- TVDashboard resets auto-pagination timer on manual navigation; shows loading state
- Models category filters show count badges; ScheduleModal includes priority/quantity/due date/notes
- Settings shows disabled Pro tabs with ProBadge for Community users instead of hiding them
- Cameras Control Room uses CSS class toggle instead of DOM manipulation
- Camera keyboard shortcut changed from bare `f` to Shift+F
- Consumables styled with Tailwind classes instead of CSS variables
- Modal accessibility improved across ~15 modals: Escape key, backdrop click-to-close
- Products BOM components editable in edit mode (previously locked after creation)
- Spool filament library material dropdown synced with all supported material types

---

## [1.3.24] - 2026-02-13

### Added
- **Timeline click-to-inspect** — clicking any timeline job block opens a slide-out detail drawer; scheduled jobs show colors, schedule, priority, filament type, pricing, match score, failure info, and notes; MQTT jobs show progress, layers, temps, duration, and error codes
- **DetailDrawer component** — reusable right-side slide-out panel (420px, mobile full-width) with backdrop, Escape-to-close, used by Timeline
- **CameraModal live print context** — `large` mode shows compact info bar (job name, progress %, nozzle/bed temps, layer info); `fullscreen` mode shows full sidebar with printer status, active job, and filament panels
- **PrinterPanels shared component** — extracted `PrinterInfoPanel`, `FilamentSlotsPanel`, `ActiveJobPanel` from CameraDetail for reuse across CameraModal and future views
- **`mqtt_job_id` on TimelineSlot** — backend schema and endpoint now expose MQTT print_jobs row ID, enabling frontend detail lookups for MQTT-tracked timeline items
- **`printJobs.get(id)`** — new API client method for fetching individual MQTT print job details

### Changed
- CameraModal fetches fresh printer telemetry (15s refetch) instead of relying on potentially stale prop data
- Timeline blocks for completed/failed/printing jobs now show `cursor-pointer` and are clickable; drag-to-reschedule still works for scheduled/pending blocks (< 5px movement = click, more = drag)

---

## [1.3.23] - 2026-02-13

### Added
- **Local dev/test/release pipeline** — `Makefile` with `build`, `test`, `deploy`, `bump`, `release`, `logs`, `shell` targets; `ops/deploy_local.sh` runs full pipeline (build → Phase 0 → pytest) on Mac via Docker Desktop
- **`ops/deploy_local.sh`** — auto-creates test virtualenv (PEP 668 safe), splits RBAC tests automatically, excludes E2E/playwright tests
- **`phase0_verify.sh` local mode** — auto-detects macOS, treats `local` same as `sandbox` (skip prod guardrails)

### Fixed
- **Login redirect loop (SB-3)** — `fetchAPI` 401 handler redirected to `/login` even when already on `/login`; `OrgProvider` calling `/auth/me` on mount triggered infinite loop
- **Setup endpoints open after admin creation (SB-3)** — `/api/setup/printer`, `/api/setup/complete`, `/api/setup/test-printer` only checked `setup_complete` flag, not whether users exist; added `_setup_is_locked()` guard
- **License feature gating for upgraded tiers** — `has_feature()` only checked license payload features, ignoring features added to the tier definition after license issuance (e.g., `usage_reports` on Pro)
- **`/api/settings` 404 on Settings page** — dead `fetch` to nonexistent endpoint removed (host IP already fetched from `/api/setup/network`)
- **`/api/alert-preferences` 500** — stale lowercase enum values (`print_complete`) in DB conflicted with SQLAlchemy enum names (`PRINT_COMPLETE`); cleaned prod DB
- **`/api/models?org_id=[object Object]` 422 on Jobs page** — added type guard in `models.list()` to reject non-primitive orgId
- **`bump-version.sh` macOS compatibility** — `sed -i` fails on BSD sed; added `sedi` wrapper for cross-platform support
- **`phase0_verify.sh` macOS compatibility** — `${VAR^^}` (bash 4+) replaced with `tr`, `head -n -1` (GNU) replaced with `sed '$d'`
- **Phase 0D false failures** — checked env vars for secrets that are stored as dotfiles (`/data/.encryption_key`, `/data/.jwt_secret`), not environment variables

### Changed
- Sandbox server (.200) no longer required for releases; optional for hardware staging
- Release pipeline: `make deploy` → `make release VERSION=X.Y.Z` runs entirely on Mac
- CLAUDE.md and server topology updated to reflect local-first workflow

---

## [1.3.19] - 2026-02-13

### Added
- **Model versioning revert** — `POST /api/models/{id}/revisions/{rev}/revert` restores a previous revision; jobs now track `model_revision_id`; revision picker in job creation form
- **Bulk operations on Printers and Spools pages** — multi-select UI with bulk update endpoint for spools (matching existing Jobs bulk ops pattern)
- **Report runner daemon** (`report_runner.py`) — background process for scheduled report execution and quiet hours digest emails; supervisord entry added
- **Organization resource scoping** — `org_id` filtering on printers, models, jobs, and spools list endpoints; shared printer flag; `OrgContext` provider; org switcher in sidebar for superadmins

### Fixed
- **Hardcoded credentials removed** — `tests/.env.test` removed from tracking; `ADMIN_PASSWORD` required via env var across all test files (no silent defaults)
- Placeholder values for IPs and access codes in `go2rtc.yaml.example`, `adapters.py`, `bambu_adapter.py`
- Report type name mismatch between frontend and backend

---

## [1.3.18] - 2026-02-13

### Added
- **Proactive stale schedule cleanup** — scheduler resets SCHEDULED jobs >2hrs past their window back to PENDING on every run (complements reactive per-printer cleanup in mqtt_monitor)
- `RoleGate` frontend component for route-level RBAC enforcement

### Fixed
- **Settings/Audit RBAC bypass** — viewers could navigate directly to `/settings` or `/audit` URLs; now redirected to dashboard
- **`create_model` dropping fields** — `quantity_per_bed`, `units_per_bed`, `markup_percent`, `is_favorite`, `thumbnail_b64` silently dropped on model creation (broke order math batching)
- `install/install.sh` version synced to current release; `bump-version.sh` now updates it automatically
- 13 test failures resolved: license gating imports, MQTT fixture schema, E2E selectors, mobile token injection
- Test integrity: security-relevant `xfail` markers converted to hard assertions, phantom test fixed, stale `xfail` removed
- E2E test `FRONTEND_URL` default corrected from `:3000` to `:8000`
- Doc inaccuracies: version attribution, test counts, wording fixes across CHANGELOG/FEATURES/ROADMAP

### Changed
- Test suite: 1022 passing (was 1007 with 13 failures), 0 xpassed (was 1)

---

## [1.3.17] - 2026-02-13

### Fixed
- **Rate limiter bug** — successful logins no longer count toward the 10/5min IP rate limit (only failed attempts count)
- RBAC test expectations aligned with backend: `POST /api/printers` → operator, `PATCH /api/orders` → admin
- Security rate-limit tests use throwaway usernames to avoid locking out real accounts
- Test fixture handles stale test users by resetting passwords if login fails
- Test session restarts backend before/after to clear in-memory lockouts

### Changed
- `docker-compose.yml` uses `build:` for sandbox (source builds, not GHCR pull)

---

## [1.3.16] - 2026-02-13

### Added
- **Curl-pipe-bash installer** (`install.sh`) — preflight checks, interactive config, image pull, health wait, non-TTY mode for automation
- **Self-updating updater** (`update.sh`) — version diffing, `--force` flag, rollback instructions, self-update before run
- Installer test suite (59 unit + integration tests) via `tests/test_installer.sh`

### Fixed
- Setup/complete endpoint 500 error when completing first-run wizard
- Security hardening: eval removal, `.env` file permissions (0600), SIGINT traps in installer scripts

### Changed
- `usage_reports` feature added to Pro license tier
- `PRINTER_ERROR` alert type added to AlertTypeEnum
- Maintenance log and moonraker monitor job status strings uppercased to match enum convention

---

## [1.3.15] - 2026-02-13

### Changed
- Sandbox test config added (`tests/.env.test`)

---

## [1.3.14] - 2026-02-12

### Fixed
- Deadlock in job matching when multiple monitors trigger scheduling simultaneously
- Race condition in concurrent schedule writes
- Timezone bugs in job matching (UTC vs local offset in time-window comparisons)

---

## [1.3.13] - 2026-02-12

### Added
- Time-window job matching strategy — scheduler evaluates availability windows instead of single-point checks
- Stale schedule cleanup — removes orphaned schedule entries that outlived their time window

---

## [1.3.12] - 2026-02-12

### Fixed
- Auto-created jobs missing required field defaults (priority, color, material)

---

## [1.3.11] - 2026-02-12

### Fixed
- Alert dispatch consistency: status types uppercased to match enum, `is_read` defaults to `False`

---

## [1.3.10] - 2026-02-12

### Added
- Auto-create jobs on print completion for reprint tracking

### Fixed
- Alert timezone handling (UTC storage, local display)
- Various alert dispatch edge cases

---

## [1.3.9] - 2026-02-12

### Fixed
- `print_jobs` table migration for upgrades from older schemas (adds missing columns idempotently)

---

## [1.3.8] - 2026-02-12

### Fixed
- Bambu pause/resume MQTT commands (correct topic and payload format)
- Job tracking state machine on pause/resume transitions

---

## [1.3.7] - 2026-02-12

### Reverted
- Camera auto-discovery for A1/P1S (caused RTSP connection loops on printers without camera support)

---

## [1.3.6] - 2026-02-12

### Fixed
- Unique MQTT `client_id` per connection to prevent broker reconnect loops when multiple monitors connect

---

## [1.3.5] - 2026-02-12

### Fixed
- Printer controls (pause/resume/stop) routing for multi-protocol setups
- Camera progress overlay alignment
- Multi-protocol command dispatch (correct adapter selection by printer type)

---

## [1.0.8] - 2026-02-10

### Fixed
- Network auto-detect now reads Host header from browser request instead of container internal IP
- Docker deployments correctly detect LAN IP in setup wizard (was showing 172.x Docker IP)
- Removed unnecessary auth headers from setup network fetch (endpoint is pre-auth)

---

## [1.0.7] - 2026-02-10

### Added
- **GHCR Docker image publishing** — pre-built images at `ghcr.io/hughkantsime/odin`
- GitHub Actions CI/CD workflow for automated image builds on tag push
- `install/docker-compose.yml` — one-command install file for end users
- Dev/prod environment separation (build from source vs pull image)

### Changed
- Production deployments now use pre-built GHCR images (no build step required)
- Install reduced to: `curl` + `docker compose up -d` (~30 seconds)

---

## [1.0.6] - 2026-02-10

### Added
- **Network Configuration UI** for camera streaming host IP
- Network step in first-run setup wizard
- Network tab in Settings page
- `GET/POST /api/setup/network` endpoints
- `sync_go2rtc_config` priority chain: env var → system_config → auto-detect
- Host IP stored in system_config table (no .env editing required)

---

## [1.0.5] - 2026-02-09

### Fixed
- Docker container startup and camera streaming configuration
- go2rtc WebRTC ICE candidate host IP configuration
- Supervisord process management for all monitor services

---

## [1.0.4] - 2026-02-09

### Fixed
- Docker entrypoint database table creation for all required tables
- Frontend build included in Docker image

---

## [1.0.3] - 2026-02-09

### Added
- Docker single-container deployment with supervisord
- Auto-generated secrets on first run
- Health check endpoint

---

## [1.0.2] - 2026-02-09

### Added
- GitHub repository made public
- README badges (version, tests, license, python, RAM)
- SECURITY.md, CONTRIBUTING.md, CHANGELOG.md for public release
- Landing page deployed to Cloudflare Pages (runsodin.com)

### Fixed
- Settings JWT auth — all Settings API calls now include Bearer token
- LicenseContext.atUserLimit — added missing `atUserLimit()` and `maxUsers`

### Changed
- Git history orphaned to single clean commit
- 47 dev/test scripts removed from tracked files (105 → 85 files)

---

## [1.0.1] - 2026-02-09

### Fixed
- License upload: Bearer token added to upload/delete requests
- License format: handle JSON format from `generate_license.py`
- Ed25519 public key: fixed truncated PEM ASN.1 header
- License expiry: parse ISO datetime with time component
- WebSocket proxy: NPM forward port changed 3000 → 8000

---

## [1.0.0] - 2026-02-09

### Security
- **53 security findings resolved** (7 ship-blocking, 5 high, 12 medium, 8 low)
- RBAC enforcement on all 164 API endpoints (viewer/operator/admin, 334 tests passing)
- JWT secret unification — OIDC and local auth use same signing key
- Setup endpoints locked after completion (SSRF prevention)
- `/label` auth bypass fixed (exact path matching)
- Branding auth bypass fixed (GET-only unauthenticated)
- User update column whitelist (SQL injection prevention)
- Admin-only access on audit logs and backup downloads
- Ed25519 keypair generated and public key embedded in production
- Dev license bypass removed

### Added
- 1,031-test QA suite (RBAC, security, features, UI, E2E)
- Landing page refresh with updated features and pricing
- Square payment integration (monthly + annual checkout)

---

## [0.19.0] - 2026-02-07

### Added
- PrusaLink printer integration (MK4/S, MK3.9, MK3.5, MINI+, XL, CORE One)
- Elegoo SDCP printer integration (Centauri Carbon, Neptune 4, Saturn)
- Smart plug frontend UI (printer settings panel)
- Picture-in-picture camera mode (draggable, resizable)
- Printer utilization report with charts and CSV export
- AMS environment chart component
- Energy tracking in analytics dashboard
- Stat cards on Orders, Products, Models, Spools pages
- REST API documentation (Swagger at /api/docs, ReDoc at /api/redoc)
- Print failure logging (reason dropdown + notes)
- Rate limiting on login (10 attempts/5min/IP)
- Account lockout (5 failed → 15min lock)
- Password complexity enforcement

---

## [0.18.0] - 2026-02-05

### Added
- Job approval workflows for Education tier
- ntfy + Telegram notification channels
- Quiet hours + daily digest
- Prometheus /metrics endpoint
- MQTT republish to external broker
- AMS humidity/temperature monitoring
- Energy consumption tracking (per-job kWh)
- HMS error decoder (42 translated Bambu codes)
- PWA manifest (add-to-homescreen)
- i18n multi-language support (EN, DE, JA, ES)
- Frontend license gating (ProGate + LicenseContext)
- Drag-and-drop queue reorder
- Keyboard shortcuts

---

## [0.17.0] - 2026-02-03

### Added
- OIDC/SSO integration (Entra ID and any OIDC provider)
- License key system (Ed25519, air-gap friendly)
- Orders, products, and BOM management
- Cost calculator with configurable pricing
- Emergency stop button
- Control room camera mode
- Browser push notifications
- Email notifications (SMTP)
- Discord/Slack webhook integration
- Custom webhooks
- White-label branding

---

## [0.16.0] - 2026-02-01

### Added
- Klipper/Moonraker printer integration (full parity with Bambu)
- WebSocket real-time updates

---

## Earlier Versions

Development versions v0.1.0 through v0.15.0 covered initial Bambu MQTT integration, dashboard, job management, spool tracking, model library, and camera streaming. These versions were pre-release and not publicly available.
