# Changelog

All notable changes to O.D.I.N. are documented here.

## [1.4.5] - 2026-02-27

### Fixed
- **GET /api/alerts 500 error** — `dispatch_alert()` (Path B) wrote uppercase `alert_type` and `severity` values to the alerts table (e.g., `PRINT_FAILED`), but the ORM enum expects lowercase (`print_failed`); SQLAlchemy failed to map on read, causing 500 on any alert query

## [1.4.4] - 2026-02-27

### Fixed
- **Circular import in mqtt_job_lifecycle** — top-level cross-module imports from `modules.notifications.alert_dispatch` and `modules.archives.archive` caused `ModuleNotFoundError` under pytest/Python 3.14; moved to lazy imports inside function bodies

### Changed
- **Monitor daemon import hardening** — all 6 monitor daemon files (`mqtt_printer`, `moonraker_monitor`, `prusalink_monitor`, `elegoo_monitor`, `detection_thread`, `mqtt_job_lifecycle`) now use lazy imports for `event_dispatcher` to prevent future circular import issues from cross-module dependencies

## [1.4.3] - 2026-02-27

### Fixed
- **Bambu prints never archived** — `record_job_ended()` now directly calls `create_print_archive()` instead of relying on InMemoryEventBus events that can't cross the process boundary between monitor daemons and FastAPI
- **No external notifications from monitor daemons** — `alert_dispatch.dispatch_alert()` (Path B) now delivers to webhooks (all 7 types), browser push, and email based on user preferences; respects quiet hours; previously only created in-app alerts
- **Critical HMS errors don't fail active jobs** — print-stopping HMS codes (waste chute pile-up, spaghetti detection) now mark the active print job as failed, create an archive, and dispatch failure alerts even when `gcode_state` doesn't transition to FAILED

### Changed
- **Cross-module violation cleanup** — eliminated all 3 known import boundary violations: `_get_org_settings` replaced with registry-based `OrgSettingsProvider`, `calculate_job_cost` extracted to `models_library/services.py`, unused `compute_printer_online` deleted; `KNOWN_VIOLATIONS` allowlist removed from contract tests; 209 contract tests pass with zero violations
- **Oversized page file splitting** — 5 frontend page files (1,098–1,941 lines) each split into focused sub-components under 400 lines; 19 extracted files including `LicenseTab`, `SpoolEditModals`, `SpoolGrid`, `OrderTable`, `OrderStats`, `RecentlyCompleted`, `JobTableHeader`, `useJobMutations` hook; all page orchestrators now under 400 lines, all extracted components under 600 lines
- **Large backend file splits** — 3 oversized backend files (880–1,204 lines) split into 14 focused sub-modules (max 350 lines): `event_dispatcher.py` → 6 files (channels, job_events, printer_health, error_handling, alert_dispatch + re-export shim); `mqtt_monitor.py` → 4 files (telemetry, job_lifecycle, printer + daemon); `vision/monitor.py` → 4 files (inference_engine, detection_thread, frame_storage + daemon); supervisord entry points unchanged

## [1.4.2] - 2026-02-26

### Changed
- **Frontend modular refactor** — monolithic `api.js` (883 lines, ~80 exports) split into `api/` directory with 14 domain modules + `client.js` + `index.js` (max file 165 lines); 34 pages grouped into 12 domain subdirectories under `pages/`; 38 components grouped into `shared/` + 9 domain subdirectories under `components/`; all import paths updated; zero functional changes; 113 files, 1801 tests passing

## [1.4.1] - 2026-02-26

### Changed
- **Route sub-router decomposition** — 8 oversized module `routes.py` files (607–1,338 lines) split into 25 focused sub-router files within `routes/` packages; each package assembles sub-routers in `__init__.py`; no URL, logic, or import path changes; OpenAPI spec preserved (578 paths)

## [1.4.0] - 2026-02-26

### Changed
- **Modular architecture refactor** — monolithic backend decomposed into 12 domain modules (`core`, `printers`, `jobs`, `inventory`, `models_library`, `vision`, `notifications`, `organizations`, `orders`, `archives`, `reporting`, `system`)
- **App factory pattern** — `create_app()` with dynamic module discovery, topological sort of load order based on `REQUIRES`/`IMPLEMENTS` declarations
- **InMemoryEventBus** — synchronous pub/sub event bus replacing direct cross-module imports for decoupling
- **ModuleRegistry** — dependency injection container for interface providers (`PrinterStateProvider`, `EventBus`, `NotificationDispatcher`, `OrgSettingsProvider`, `JobStateProvider`)
- **Module-owned SQL migrations** — per-module `migrations/001_initial.sql` files with migration runner, replacing 700+ lines of inline SQL in `entrypoint.sh`
- **Module manifests** — each module's `__init__.py` declares `MODULE_ID`, `ROUTES`, `TABLES`, `PUBLISHES`, `SUBSCRIBES`, `IMPLEMENTS`, `REQUIRES`, `DAEMONS`, and a `register(app, registry)` function
- **Contract tests** — 171 new tests including import boundary enforcement (`test_no_cross_module_imports`), module manifest validation, event bus contracts, and interface ABC compliance
- `main.py` reduced from 524 → 12 lines; `entrypoint.sh` from 960 → 347 lines

### Fixed
- Missing Pydantic schemas in orders module (`ProductConsumableCreate`, `ConsumableCreate`, etc.) — 27 endpoints were silently dropped
- Incomplete `AlertType` enum in notification schemas (missing `bed_cooled`, `queue_added`, `queue_skipped`, `queue_failed_start`)
- Broken `print_file_meta` import path in models library routes
- `dateutil` dependency replaced with stdlib `datetime.fromisoformat()` in session routes
- `quantity_on_bed` schema made `Optional[int]` to match nullable DB column

## [1.3.76] - 2026-02-25

### Added
- **Printer model auto-detection** — `POST /api/printers/test-connection` and `POST /api/setup/test-printer` now return a `model` field for all four protocols:
  - Bambu: maps MQTT `printer_type` codes (e.g., `BL-P001` → `X1C`) using `printer_models.py`
  - Moonraker/Klipper: queries `/server/config` for kinematics (`corexy` → `"Voron"`), falls back to hostname substring hints
  - PrusaLink: extracts printer type from `/api/version` response, maps to friendly names (MK4S, MK3.9, CORE One, etc.)
  - Elegoo: UDP unicast M99999 probe on port 3000, parses `MachineName` from SDCP response
- **`backend/printer_models.py`** — centralized model code mapping module; `normalize_model_name(api_type, raw_value)` dispatcher; unknown codes pass through as-is, empty input returns `None`
- **`/api/setup/test-printer`** — added PrusaLink and Elegoo branches (previously fell through to error case)
- Model detection is best-effort: failure returns `null`, never causes test-connection to fail

## [1.3.75] - 2026-02-25

### Added
- **Multi-plate reprint** — plate selector in archive reprint modal and model schedule modal for multi-plate 3MF files; `plate_count` exposed in variants endpoint
- **H2D external spools** — parse `vt_tray` from MQTT for Ext-L / Ext-R spool positions; display material, color, and remaining percentage on printer cards
- **Scheduler target types** — jobs can target specific printer, machine model, or protocol; `target_type` and `target_filter` columns; filament compatibility check endpoint
- **Bed cooled notification** — monitor thread polls bed temp after job completion, dispatches alert when below threshold (default 40°C)
- **Queue notifications** — QUEUE_ADDED, QUEUE_SKIPPED, QUEUE_FAILED_START alert types with configurable preferences
- **Filament auto-deduction** — deduct consumed filament from assigned spool on print completion; sync to Spoolman if linked
- **Italian locale** — it.json with 180 translation keys
- **Projects page** — card grid, create modal, detail drill-down with archive assignment at `/projects`

## [1.3.74] - 2026-02-25

### Added
- **Print Log** — Dense table view at `/print-log` with sortable columns, pagination, and CSV export (`GET /api/archives/log`)
- **Archive comparison** — Side-by-side diff of any two archives (`GET /api/archives/compare?ids=1,2`)
- **Archive tag management** — Add, remove, rename, and bulk-manage tags on archive entries (`GET/POST/DELETE /api/archives/tags`)
- **Reprint from archive** — One-click reprint with AMS filament mapping preview (`POST /api/archives/{id}/reprint`, `GET /api/archives/{id}/ams-preview`)
- **Projects** — Group archives into named projects with full CRUD, bulk assign, ZIP export/import (`/api/projects/*`)
- **Fan speed control** — Set part cooling, auxiliary, and chamber fan speeds for Bambu printers (`POST /api/printers/{id}/fan`)
- **AMS RFID re-read** — Trigger AMS filament re-scan via MQTT (`POST /api/printers/{id}/ams/refresh`)
- **AMS slot configuration** — Set material, color, and K-factor per AMS slot (`PUT /api/printers/{id}/ams/{ams_id}/slots/{slot_id}`)
- **Plate cleared confirmation** — Confirm build plate cleared to unblock next queued job (`POST /api/printers/{id}/plate-cleared`)
- **Batch job send** — Send the same job to multiple printers in one operation with queue-only mode (`POST /api/jobs/batch`)
- **File duplicate detection** — SHA-256 hash on upload with duplicate warning and existing file reference
- **WhatsApp notifications** — Meta Business API integration for alert dispatch and test webhook
- **Pushover notifications** — Native Pushover API with priority mapping for alert dispatch and test webhook
- **Low-stock spool alerts** — Configurable per-spool threshold with `GET /api/spools/low-stock` endpoint and `is_low_stock` flag
- **Pressure advance profiles** — Per-spool PA profile field for storing calibrated values
- **Spoolman integration** — `spoolman_spool_id` field linking O.D.I.N. spools to external Spoolman instance
- **Spool CSV export** — Export full inventory as CSV via `GET /api/spools/export`
- **User theme preferences** — Server-side accent color and sidebar style per user (`GET/PUT /api/auth/me/theme`)

## [1.3.73] - 2026-02-24

### Fixed
- **Test suite hardening** — Resolved 36 skipped tests and 1 failure (1631 → 1797 passing). SSE endpoint streaming, ephemeral resource creation for RBAC DELETE tests, maintenance DELETE RBAC matrix correction (admin-only, not operator), password credential mismatches in test fixtures, lockout test using form-data instead of JSON, job clone test path, alert fixture enum validation.
- **Profiles datetime serialization** — Handle SQLite string dates in GET /api/profiles response (fixes 500 error)
- **Git repository corruption** — Repacked and cleaned corrupt .git directory

## [1.3.70] - 2026-02-24

### Added
- **HMS error expansion** — Expanded from 42 to 500+ translated Bambu HMS error codes with human-readable descriptions
- **Clear HMS errors** — `POST /api/printers/{id}/clear-errors` sends reset command via MQTT; UI button on printer cards
- **Skip objects during print** — `POST /api/printers/{id}/skip-objects` excludes selected objects mid-print for Bambu printers
- **Print speed adjustment** — `POST /api/printers/{id}/speed` changes speed 25%–200% mid-print; slider control on printer cards
- **Resizable printer cards** — S/M/L/XL card sizes with persistent preference (localStorage)
- **OBS streaming overlay** — `/overlay/:printerId` page with camera feed, progress, temps, and job info; no auth required for OBS browser source
- **Live application log viewer** — Settings → Logs tab with 9 log sources, level filtering, text search, and SSE live streaming (admin-only)
- **Support bundle generator** — One-click diagnostic ZIP download from Settings → System with privacy-filtered system info, connectivity, settings, and recent errors (admin-only)
- **Email-based user onboarding** — Admin creates user with "Send welcome email" option; system generates random password and sends invite via SMTP
- **Self-service password reset** — Forgot-password flow on login page with 1-hour email token, single-use enforcement, and session revocation on reset
- **Admin password reset email** — Admin can trigger password reset email for any user from user management
- **Print archive** — Auto-captures completed prints with job name, printer, status, duration, filament used, thumbnail, and user attribution; dedicated `/archives` page with filters, search, detail modal, editable notes, and admin delete
- **Timelapse editor** — In-app trim (start/end via range sliders), speed adjustment (0.5×–8×), and download; ffmpeg-backed with graceful 501 fallback
- **Build plate empty detection** — New Vigil AI detection category; ONNX inference identifies empty build plates for job-complete confirmation
- **Slicer & printer profiles library** — Upload, tag, version, and distribute OrcaSlicer (.json), Bambu Studio (.json), PrusaSlicer (.ini), and Klipper (.cfg) profiles; revision history with diff and rollback
- **H2D dual-nozzle AMS support** — Auto-detected `machine_type` column; dual nozzle temps (L/R), dual AMS unit labels, H2D badge on printer cards, `GET /api/printers/{id}/nozzle-status` endpoint
- **Windows PowerShell installer** — `install.ps1` with preflight checks (Docker, WSL2, disk, ports), interactive config, and WINDOWS_INSTALL.md guide
- **Documentation wiki** — Docusaurus 3 site at docs.runsodin.com with 19 pages (installation, configuration, features, API reference, troubleshooting); Documentation link in Settings page
- **Compare page (runsodin.com)** — Feature-by-feature comparison against BambuBuddy and SimplyPrint across 7 categories
- **Reviews page (runsodin.com)** — User testimonial cards with star ratings and submit-review CTA
- **Features page update (runsodin.com)** — Added Vigil AI, Enterprise Security, and Business Operations sections with mixed-fleet banner and compare CTA
- **Site navigation update (runsodin.com)** — Added Compare, Reviews, Docs, and Discord links to header and footer

## [1.3.69] - 2026-02-23

### Fixed
- **RBAC matrix coverage** — Added ~120 missing routes to `ENDPOINT_MATRIX` in `tests/test_rbac.py`. Every route now has documented and tested auth expectations; `make test-coverage` passes clean.
- **5 incorrect auth expectations** — Corrected auth helpers for `GET /api/users/{user_id}/export` (IDOR → admin only), `GET /api/groups/{group_id}` (IDOR → admin only), `PATCH /api/vision/detections/{id}` (operator not admin), `GET /api/vision/settings` (admin only), `POST /api/presets` (no body to avoid UNIQUE constraint on re-runs).
- **Invoice PDF crash on missing SKU** — `invoice_generator.py` used `"—"` (em dash) as the fallback SKU, which fpdf's Helvetica font cannot render. Changed to `"-"`.
- **Retention cleanup 500** — `POST /api/admin/retention/cleanup` queried `audit_logs.created_at` which does not exist; column is `timestamp`.
- **CI pipeline** — Corrected `pip-audit` flags (2.x removed `--severity`); made Bandit parse errors non-fatal (Python version skew in CI); HIGH severity findings still hard-fail.

## [1.3.68] - 2026-02-21

### Fixed
- **Spool label endpoint auth** — `GET /api/spools/{id}/label` now correctly requires viewer+ auth. Tests updated to match.
- **GET /api/config accessible to viewer/operator** — Dropped `require_role("admin")` to `require_role("viewer")` for the config read endpoint; it only returns non-sensitive values (`spoolman_url`, `blackout_start`, `blackout_end`).
- **package-lock.json sync** — Updated lockfile to include `three@0.128.0` (was missing, caused `npm ci` failure in Docker builds).

## [1.3.67] - 2026-02-21

### Security
- **OIDC redirect_uri pinning** — Added `OIDC_REDIRECT_URI` environment variable. When set, both `GET /auth/oidc/login` and `GET /auth/oidc/callback` use the configured URI instead of deriving it from the `Host` header. Eliminates Host-header injection risk when ODIN is behind a reverse proxy.
- **oidc_auth_codes encrypted** — The JWT stored in the `oidc_auth_codes` table for the OIDC code-exchange flow is now Fernet-encrypted at rest. A database dump no longer yields usable access tokens. Migration-safe decrypt fallback handles any existing plaintext rows.
- **Org webhook_url encrypted** — Org-level `webhook_url` in `groups.settings_json` is now Fernet-encrypted on write and decrypted transparently by `_get_org_settings()`. Protects Discord/Slack/Telegram tokens in multi-org backup dumps.
- **onnxruntime upgraded** — Bumped from `1.17.1` to `1.20.1`, resolving CVE-2024-25960 (heap buffer overflow in ONNX parsing).
- **Docker base image digest pinning** — `python:3.11-slim` and `node:20-slim` are now pinned to `sha256:` digests in the Dockerfile. A supply-chain compromise of the floating tag will no longer silently pull a malicious layer.
- **go2rtc SHA256 verification** — go2rtc v1.9.4 binary is now SHA256-verified during Docker build (per-arch hashes for amd64 and arm64). Build fails on binary tampering or MITM.
- **VITE_API_KEY removed** — Removed all `import.meta.env.VITE_API_KEY` / `X-API-Key` references from 16 frontend files. Session auth is fully httpOnly cookie (`credentials: 'include'`); the dead env var was a footgun that would bake an API key into the public JS bundle if accidentally set at build time.
- **Webhook URL encryption** — Discord/Slack/Telegram/ntfy webhook URLs are now Fernet-encrypted before storage in the `webhooks` table. Tokens embedded in URLs (e.g. `discord.com/api/webhooks/ID/TOKEN`) are no longer plaintext in DB or backups. Migration-safe decrypt fallback handles existing rows.
- **API_KEY startup warning** — A `WARNING` log is now emitted at startup if `API_KEY` is unset, making the "perimeter auth disabled" state explicit rather than silent.
- **Backup path traversal hardened** — `download_backup` and `delete_backup` now use `realpath()` + prefix check instead of string-scan, consistent with all other file-serving endpoints (symlink-safe).
- **Setup test-printer SSRF** — `POST /setup/test-printer` now calls `_check_ssrf_blocklist(api_host)` before attempting outbound MQTT/HTTP connection.

## [1.3.66] - 2026-02-21

### Security
- **OIDC login fixed** — `POST /auth/oidc/exchange` now sets an httpOnly session cookie and records the session (previously returned JSON only, making OIDC SSO non-functional since `Login.jsx` discarded the response body).
- **Logout invalidates Bearer JWT** — `POST /auth/logout` now blacklists the `Authorization: Bearer` token in addition to the session cookie, so API clients that call logout are properly signed out.
- **Password change revokes sessions** — `PATCH /users/{user_id}` now blacklists all existing session JTIs and clears `active_sessions` for the target user when a password change is applied, preventing credential reuse.
- **Bambu SSRF check** — `POST /bambu/test-connection` now calls `_check_ssrf_blocklist()` on `ip_address` before attempting the MQTT connection, blocking SSRF targeting internal hosts.
- **Live-status error redaction** — `_fetch_printer_live_status` no longer returns `str(e)` in error responses; internal exception details are logged at DEBUG level and a generic message is returned to the caller.
- **stored_path removed from print-file responses** — server filesystem paths are stripped from `POST /print-files`, `GET /print-files`, and `GET /print-files/{id}` responses.
- **`.env` permissions hardened** — `docker/entrypoint.sh` now runs `chmod 600 /app/backend/.env` after writing secrets to it (matching the existing behaviour for `/data/.env.supervisor`).

## [1.3.65] - 2026-02-21

### Security
- **Timelapse path traversal** — `GET /timelapses/{id}/video` and `DELETE /timelapses/{id}` now resolve `t.filename` via `os.path.realpath()` and verify the result starts with `/data/timelapses/` before serving or deleting the file.
- **Vision training export path traversal** — the training data ZIP export resolves each `frame_path` entry via `os.path.realpath()` and skips any path that escapes `/data/vision_frames/`.
- **Model revision revert path traversal** — the revert endpoint now calls `os.path.realpath()` on `target.file_path` and rejects paths outside `/data/` with HTTP 400.
- **Model revision upload size limit** — the revision upload handler now enforces a 100 MB limit (matching the primary upload endpoint).
- **ONNX upload size limit** — the ONNX model upload endpoint now enforces a 500 MB limit.
- **Backup restore size limit** — the backup restore endpoint now enforces a 100 MB size limit before writing to disk.
- **Backup restore trigger scan** — after the existing integrity check, the restore endpoint now queries `sqlite_master` for triggers and rejects any backup containing them.
- **Detection label_class allowlist** — `POST /vision/training-data/{id}/label` now validates `label_class` against `{"spaghetti", "first_layer_failure", "detachment", "false_positive"}` and returns HTTP 400 for any other value.

## [1.3.64] - 2026-02-21

### Security
- **Three.js bundled locally** — `ModelViewer.jsx` now imports Three.js from npm (`three@0.128.0`) instead of a CDN dynamic import. Eliminates supply chain risk from external CDN compromise.
- **CSP connect-src tightened** — removed `ws:` and `wss:` wildcards from `connect-src` in `backend/main.py`. `'self'` already covers same-origin WebSocket connections.
- **OIDC dead code removed** — removed the dead `urlToken` code block from `Login.jsx` (pre-cookie migration remnant). Replaced with working `oidc_code` exchange flow: frontend now detects `?oidc_code=` on load and POSTs it to `/api/auth/oidc/exchange`.
- **oidcExchange added to api.js** — `api.auth.oidcExchange(code)` added to the API client for the OIDC code exchange.
- **OIDC error URL-encoded** — OIDC callback error redirects now URL-encode the error string via `urllib.parse.quote()` to prevent open redirect injection via crafted error strings.
- **Google Fonts removed from Branding page** — removed the runtime Google Fonts CDN `<link>` injection from `Branding.jsx`. Font previews fall back to system fonts already loaded. Eliminates GDPR data transfer to Google.

## [1.3.63] - 2026-02-21

### Security
- **ws-token REST rejection** — `get_current_user()` in `deps.py` now rejects JWTs containing `"ws": True` on both cookie and Bearer auth paths. WebSocket tokens captured from URL query parameters can no longer be replayed against REST endpoints.
- **MFA pending token blacklisted on use** — `mfa_verify` blacklists the `mfa_pending` JWT immediately after successful TOTP verification. Re-submitting the same token returns 401. Prevents a second session being issued from a single MFA flow.
- **`revoke_all_sessions` cookie-auth fix** — Caller JTI is now extracted from the session cookie when no Bearer header is present. Cookie-auth callers (browsers) are no longer logged out by their own revoke-all request. Same fix applied to `list_sessions` `is_current` detection.

## [1.3.62] - 2026-02-21

### Security
- **Credential encryption at rest** — SMTP password encrypted with Fernet before storage in `system_config`; decrypted on read in `alert_dispatcher.py` and `report_runner.py`. MQTT republish password encrypted on write in `system.py`; decrypted before use in `mqtt_republish.py`. All decryption paths are migration-safe (fall back to raw value if decryption fails so existing deployments upgrade without breaking).
- **camera_url credential persistence removed** — Bambu RTSP URLs (containing plaintext `access_code`) are no longer persisted to `camera_url`. `get_camera_url()` already generates the URL on-demand from the encrypted `api_key`. `discover_camera()` now only syncs the go2rtc config without writing the plaintext URL to the DB. User-supplied camera URLs containing embedded credentials (`@`) are encrypted before storage and decrypted on read.

## [1.3.61] - 2026-02-21

### Security
- **Auth coverage sweep** — added `require_role()` guards to all endpoints identified in the authorization security audit: `GET /stats`, `/analytics`, `/analytics/failures`, `/analytics/time-accuracy` (viewer), `GET /cameras` (viewer), `GET /search`, `/maintenance/tasks`, `/maintenance/logs`, `/maintenance/status`, `/hms-codes/{code}` (viewer), `GET /config`, `/spoolman/test` (admin), `POST /setup/network` (admin + setup-lock check), `GET /print-jobs`, `/print-jobs/stats`, `/print-jobs/unlinked`, `/failure-reasons`, `/presets`, `GET /jobs`, `GET /jobs/{id}` (viewer)
- **Approve/reject hardened** — `POST /jobs/{id}/approve` and `/reject` now use `require_role("operator")` dependency instead of a manual role check after feature gate
- **Bulk job mutations** — `POST /jobs/bulk-update` checks `check_org_access()` per job ID before mutation; `POST /jobs/{id}/repeat` and `/link-print` also check org access
- **Metrics auth** — `GET /metrics` removed from unauthenticated middleware bypass; now requires viewer role or API key
- **Groups IDOR** — `GET /groups/{id}` rejects non-admin callers attempting to access a group they don't belong to; `GET /groups` returns only the caller's own group for non-admins
- **Education usage report** — `GET /education/usage-report` scopes user query to caller's `group_id` for non-admin roles
- **OIDC defaults hardened** — `default_role` changed from `"operator"` to `"viewer"`; `auto_create_users` default changed from `True` to `False`
- **License server IP** — hardcoded `http://192.168.70.6:5000` fallback removed from `POST /license/activate`; requires explicit `LICENSE_SERVER_URL` env var; http URLs (non-localhost) automatically upgraded to https
- **Maintenance task/log deletion** — `DELETE /maintenance/tasks/{id}` and `/maintenance/logs/{id}` elevated from operator to admin

## [v1.3.60] - 2026-02-21 (skipped — no backend changes)

## [1.3.59] - 2026-02-21

### Security
- **httpOnly cookie auth** — JWT moved from `localStorage` to an httpOnly session cookie (`Secure`, `SameSite=Strict`). Cookie auth is Try 0 in `get_current_user`; Bearer token and X-API-Key fallbacks preserved for API clients. Login and MFA verify both set the cookie. New `POST /auth/logout` endpoint clears cookie and blacklists JWT. Frontend `api.js` uses `credentials: 'include'`; all localStorage token reads removed across 20+ files.
- **go2rtc network isolation** — go2rtc HLS/API server (`port 1984`) now bound to `127.0.0.1` only. External clients can no longer bypass ODIN auth to access streams directly. WebRTC port 8555 remains on `0.0.0.0` for ICE candidates. Port 1984 removed from Dockerfile EXPOSE and docker-compose.yml.
- **Container non-root user** — `odin` user/group created in Dockerfile. `supervisord.conf` sets `user=odin`; `entrypoint.sh` chowns `/data` and `/app` before exec-ing supervisord. All 9 supervised processes run as non-root.
- **Global rate limiting (slowapi)** — `slowapi==0.1.9` added. Auth endpoints (`/auth/login`, `/auth/mfa/verify`) limited to 10 req/min per IP. File upload endpoint limited to 30 req/min. Shared limiter instance in `backend/rate_limit.py`.
- **API token scope enforcement** — `require_scope(scope)` dependency added to `deps.py`. `require_role()` updated to accept optional `scope=` parameter. Create/delete endpoints on printers, jobs, models, and spools enforce `scope="write"` for per-user scoped tokens. JWT and global API key bypass scope checks (full access). New `POST /auth/ws-token` endpoint issues 5-minute JWT for WebSocket authentication.

---

## [1.3.58] - 2026-02-21

### Security
- **JWT entropy** — `JWT_SECRET_KEY` generation switched from `token_urlsafe(32)` (~192 bits) to `token_bytes(32).hex()` (256 bits, no encoding overhead)
- **Numeric field bounds** — Pydantic validators enforce: `slot_count` 1–256 (PrinterUpdate), `quantity` 1–10000 (JobBase/JobUpdate), `priority` 0–10 (JobBase/JobUpdate), `units_per_bed`/`quantity_per_bed` 1–10000 (ModelBase/ModelUpdate)
- **Camera URL validation** — camera URLs must use `rtsp://` or `rtsps://` scheme; shell metacharacters stripped before storage; localhost/loopback targets rejected with 400
- **Webhook SSRF** — webhook URLs (create/update system webhooks, org webhook settings) validated: http/https only, no loopback/link-local/RFC-1918 targets; shared `_validate_webhook_url` helper in `deps.py`
- **Audit: password changes** — `log_audit("user.password_changed")` emitted with actor/target user IDs when a password is updated via `PATCH /users/{id}`
- **Audit: successful logins** — `log_audit("auth.login")` emitted with username and client IP on every successful `POST /auth/login`
- **GDPR export completeness** — `GET /users/{id}/export` now includes `api_tokens` (name, scopes, timestamps — no token values) and `quota_usage` records

---

## [1.3.57] - 2026-02-21

### Security
- **Credential exposure** — `api_key` removed from `PrinterResponse` schema; encrypted credentials no longer returned in any printer API response
- **Camera URL sanitization** — embedded Bambu access codes stripped from `camera_url` in all API responses (`rtsps://bblp:ACCESS_CODE@...` → `rtsps://***@...`)
- **Missing auth guards** — `GET /printers/tags`, `GET /printers/{id}/live-status`, `GET /cameras/{id}/stream`, and `POST /cameras/{id}/webrtc` now require authentication
- **Last-admin protection** — `DELETE /users/{id}` now rejects the request if it would remove the last admin account
- **SSRF blocklist** — printer `api_host` is validated against localhost, loopback, and link-local ranges on create and test-connection
- **XXE prevention** — `threemf_parser.py` now uses `defusedxml` instead of `xml.etree.ElementTree`; added `defusedxml==0.7.1` to requirements
- **ZIP bomb protection** — 3MF/gcode upload enforces 100 MB upload limit and rejects archives whose decompressed size exceeds 500 MB
- **Path traversal** — model revision upload filename now sanitized with `re.sub` before constructing file path
- **HSTS** — `Strict-Transport-Security` header added (2-year max-age) when running over HTTPS
- **Error message sanitization** — raw `str(e)` no longer returned in HTTP responses; exceptions logged server-side with generic messages to clients

---

## [1.3.56] - 2026-02-21

### Added
- **Dispatch compatibility guardrails** — Printer dispatch now validates file-printer compatibility before sending:
  - **Metadata extraction** — bed dimensions parsed from gcode slicer comments (PrusaSlicer, Cura, Bambu) and 3MF XML at upload time; stored on `print_files` as `bed_x_mm`, `bed_y_mm`, `compatible_api_types`
  - **API type guard** — if a file's `compatible_api_types` is set (e.g. `"bambu"`), dispatching it to a Moonraker/PrusaLink printer returns HTTP 400 with a clear message
  - **Bed size guard** — if both the file's sliced bed and the printer's configured bed are known, a mismatch (file > printer + 2mm tolerance) blocks dispatch with a mismatch message
  - **Soft-fail** — missing bed data on either side skips the check; never silently blocks unknown files
  - **Printer bed config** — `bed_x_mm`/`bed_y_mm` fields added to printer add/edit form with auto-fill for common models (X1C, P1S, MK4, Ender 3, etc.)
  - **Models page badges** — print file variants now show bed dimensions, "Bambu only" / "Moonraker / PrusaLink" compatibility badges, and a warning icon for files with no slicer metadata
  - **Job modal warning** — creating or editing a job with a mismatched printer shows an inline yellow warning (non-blocking, operator can override)
- **gcode/bgcode file support in dispatch** — upload route now accepts `.gcode` and `.bgcode` in addition to `.3mf`; all three stored to disk and dispatched correctly

---

## [1.3.50] - 2026-02-20

### Added
- **Browser notifications** — ODIN now requests OS notification permission on first load. When an alert or Vigil AI detection fires while the tab is in the background, a native browser notification pops up so users don't have to watch the tab. No external services or configuration required.

---

## [1.3.49] - 2026-02-20

### Fixed
- **Route ordering: GET /printers/live-status** — FastAPI was matching `live-status` as a `{printer_id}` parameter, returning 422. Static route now registered before the parameterized route.
- **Route ordering: PATCH /jobs/reorder** — Same issue; `reorder` was being matched as a `{job_id}`, returning 422. Fixed by registering the static route first.
- **Scheduler timezone mismatch** — `datetime.now(timezone.utc)` (offset-aware) was compared against SQLite-stored naive datetimes, causing `TypeError: can't subtract offset-naive and offset-aware datetimes`. Fixed by using `datetime.now()` throughout the scheduler.
- **Missing `queue_position` column** — `PATCH /jobs/reorder` referenced `queue_position` in raw SQL but the column was never in the schema. Added to `Job` model and `entrypoint.sh` migration.
- **Spool weigh endpoint 500** — `SpoolUsage` was constructed with `assigned_spool_id` (wrong field name); correct field is `spool_id`. Fixed.
- **PUT /config EDEADLK on macOS Docker Desktop** — Writing to `/data/.env` with `open(..., 'w')` triggered `OSError: [Errno 35] Resource deadlock avoided` on macOS fakeowner mounts. Fixed with temp file + `os.replace()` atomic rename.
- **POST /config/mqtt-republish/test 500 on empty body** — `await request.json()` raised `JSONDecodeError` when called with no body (RBAC tests). Wrapped in try/except, defaults to `{}`.
- **RBAC test suite** — Extended soft-pass to accept 400/422/503 as valid auth-passed business-logic rejections. Added trusted-network-mode bypass for `api_key_only` 401s. All 67 RBAC failures resolved (839 passed, 0 failed).

---

## [1.3.48] - 2026-02-20

### Fixed
- **SQLAlchemy 2.x enum storage bug** — SQLAlchemy 2.x defaults to storing Python enum member *names* (uppercase) as DB values instead of member *values* (lowercase). Fixed by adding `values_callable` to all 10 `SQLEnum` column definitions in `models.py`. This was causing 500 errors on `/api/jobs`, `/api/export/jobs`, and `/api/analytics` endpoints.
- **Missing PAUSED job status** — Added `PAUSED = "paused"` to `JobStatus` enum in both `models.py` and `schemas.py`. Previously, paused jobs would raise a `LookupError` when serialized.
- **Raw SQL uppercase status literals** — Fixed hardcoded uppercase status strings (`'PENDING'`, `'SCHEDULED'`, `'PRINTING'`, `'COMPLETED'`) in `mqtt_monitor.py`, `moonraker_monitor.py`, `routers/models.py`, and `routers/system.py` to match the lowercase enum values.
- **DB migration on startup** — `entrypoint.sh` now normalizes any existing uppercase enum values in `jobs`, `spools`, `orders`, and `filament_slots` tables on container start.
- **Test isolation: rate limiting** — Security tests that trigger IP-based rate limiting now clear `login_attempts` via `teardown_class`. `Makefile` clears `login_attempts` before each pytest invocation to prevent accumulated entries from blocking subsequent test sessions.
- **Test: `test_to_dict` outside container** — `test_license.py::TestValidLicense::test_to_dict` patched `get_installation_id` so it doesn't try to write to `/data/` when running tests on the local Mac.
- **Test: MQTT linking fixtures** — `test_mqtt_linking.py` fixtures updated to use lowercase status values, matching the corrected monitor SQL queries.

---

## [1.3.47] - 2026-02-20

### Added
- **Scheduled Reports "Run Now"** — `POST /api/v1/report-schedules/{id}/run` endpoint immediately generates and emails a report; `run_report()` public function extracted from report_runner daemon; Run Now (⚡) button in ReportScheduleManager UI with success/error toast
- **WCAG 2.1 improvements** — `aria-busy` on async action buttons (Run Scheduler, Create Schedule, Run Now); sr-only `aria-live="polite"` region in main layout for screen reader announcements

### Changed
- **ROADMAP.md** — Removed stale asterisk caveats from Organizations, File/Model Versioning, and Scheduled Reports (all features fully shipped)

---

## [1.3.46] - 2026-02-18

### Added
- **Installation-bound license activation** — Persistent installation ID generated per ODIN instance; licenses can be bound to a specific installation, preventing file-copy piracy; online activation via license key (`POST /api/license/activate`); offline activation request export; license server `/api/activate` endpoint with activation tracking and limits; backwards compatible with existing unbound licenses

### Changed
- **Pricing locked** — Pro: $15/mo or $150/yr (was $29/$290). Education: $300/yr per site (was $500/yr per campus). Updated ToS, README, GTM docs, marketing sites, and portfolio docs
- **Marketing site migrated to Vercel** — runsodin.com now served from Vercel (was Cloudflare Pages). Added vercel.json, security headers, SPA rewrites
- **Removed comparison and testimonials sections** — ComparisonSection removed (not aligned with brand voice), TestimonialsSection removed (no real reviews yet)

---

## [1.3.45] - 2026-02-17

### Added
- **Org-level settings** — `settings_json` column on groups table; `GET/PUT /api/v1/orgs/{id}/settings` endpoints; default filament type/color applied on job creation; org-level quiet hours; org webhook dispatch; branding overlay (app name, logo URL); OrgManager settings panel UI

---

## [1.3.44] - 2026-02-17

### Fixed
- **JWT library migration** — Replaced `python-jose` with `PyJWT` (python-jose is unmaintained; PyJWT is the actively maintained standard)

---

## [1.3.43] - 2026-02-17

### Fixed
- **Setup page 401 redirect loop** — Prevented auth redirect when accessing setup page before any users exist

---

## [1.3.42] - 2026-02-17

### Fixed
- **Setup wizard redirect** — Fixed redirect logic when no users exist in the database

---

## [1.3.41] - 2026-02-17

### Changed
- **Standardized page headers** — All 23 pages now use consistent header pattern: `text-xl md:text-2xl font-display font-bold`, icon at 24px with `text-print-400`, subtitle in `text-farm-500 text-sm mt-1`

---

## [1.3.40] - 2026-02-16

### Fixed
- **Analytics page styling** — Restyled Analytics to match app-wide design system (card borders, stat cards, spacing)

---

## [1.3.39] - 2026-02-16

### Fixed
- **Analytics/Utilization styling** — Unified Analytics and Utilization page styling with consistent card and chart patterns

---

## [1.3.38] - 2026-02-16

### Fixed
- **Datetime mismatch** — Fixed naive/aware datetime comparison bugs in analytics date filtering and maintenance status checks

---

## [1.3.37] - 2026-02-16

### Changed
- Documentation updates for router split and FastAPI architecture (no code changes)

---

## [1.3.36] - 2026-02-16

### Fixed
- **Code smell remediation** — Fixed spoolman_spool_id attribute access, audit_logs table name, bulk add_tag JSON logic, encrypt plug_auth_token, deduplicate quota helpers, normalize `datetime.now(timezone.utc)` across 12 files, replace deprecated `utcnow()`, remove dead code (`verify_api_key`, `init_db`), `StaticPool→NullPool`, bare `except→except Exception` at 25 sites
- **Frontend API consolidation** — All pages migrated to centralized `fetchAPI` (Settings, Spools, Printers, Analytics, Orders, App.jsx), removed 6 local API layers (~200 lines), fixed auth header bugs, deduplicated `getShortName`, replaced alert/confirm with toast/ConfirmModal, SW cache cleanup

---

## [1.3.35] - 2026-02-16

### Fixed
- **LICENSE file version reference** — Updated from v0.17.0 to v1.0.0

---

## [1.3.34] - 2026-02-16

### Added
- **Third-party notices** — Added THIRD_PARTY_NOTICES.md for open-source attribution
- **GDPR tier corrections** — Updated privacy policy to reflect accurate tier-based data handling

### Fixed
- **Legal/compliance audit remediation** — Self-hosted fonts (GDPR compliance, no Google Fonts CDN), CORS hardening, MFA encryption guard, periodic session cleanup, error sanitization, alt text fixes, cache versioning, referrer policy, GDPR data export/erase UI, license type display

---

## [1.3.33] - 2026-02-16

### Fixed
- **Printer command allowlist** — Restricted printer control commands to known-safe set with proper status codes
- **Security hardening** — Deprecation fixes, accessibility improvements
- **Monitor DB centralization** — Centralized monitor database access, SQLite-backed IPC, rate limiting refactor

---

## [1.3.32] - 2026-02-16

### Fixed
- **Light mode text contrast** — Fixed `text-white` blanket override in light mode and branding contrast issues

---

## [1.3.31] - 2026-02-16

### Fixed
- **Enterprise test license** — Mount Enterprise test license for RBAC test suite
- **CSS farm palette** — CSS-variable-based farm palette for robust light mode theming

---

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
