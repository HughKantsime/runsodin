# Changelog

All notable changes to O.D.I.N. are documented here.

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

### Changed
- `usage_reports` feature moved from Education-only to Pro license tier
- `PRINTER_ERROR` alert type added to AlertTypeEnum
- Maintenance log and moonraker monitor job status strings uppercased to match enum convention

---

## [1.3.15] - 2026-02-13

### Added
- **Curl-pipe-bash installer** (`install.sh`) — preflight checks, interactive config, image pull, health wait, non-TTY mode for automation
- **Self-updating updater** (`update.sh`) — version diffing, `--force` flag, rollback instructions, self-update before run
- Installer test suite (42 unit + integration tests) via `tests/test_installer.sh`

### Fixed
- Setup/complete endpoint 500 error when completing first-run wizard
- Security hardening: eval removal, `.env` file permissions (0600), SIGINT traps in installer scripts

### Changed
- Sandbox test config added (`tests/.env.test`)
- `usage_reports` feature moved from Education-only to Pro tier
- `PRINTER_ERROR` alert type added to AlertTypeEnum
- Maintenance log and moonraker monitor job status strings uppercased to match enum convention

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
