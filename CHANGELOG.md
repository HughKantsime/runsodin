# Changelog

## [1.0.0] — 2026-02-09

### 🎉 O.D.I.N. 1.0 — First Public Release

**Security Audit & Hardening (53 findings resolved):**
- RBAC enforcement on all mutating API routes (SB-1)
- JWT secret unification — OIDC and local auth use same signing key (SB-2)
- Setup endpoints locked after initial setup completes (SB-3)
- Auth bypass fixes: `/label` path matching, branding endpoint protection (SB-4, SB-5)
- User update column whitelist prevents SQL injection via column names (SB-6)
- Frontend branding defaults corrected to O.D.I.N. (SB-7)
- Duplicate route definitions removed (H-1)
- Version string consistency across all endpoints (H-2)
- Password validation enforced on user updates (M-3)
- Admin RBAC on audit logs and backup downloads (M-7, M-11)
- Setup password validation indentation fix (L-1)

**License System (Production-Ready):**
- Ed25519 keypair generated and embedded
- Dev bypass removed — all licenses cryptographically verified
- License generation and verification workflow validated end-to-end
- Founders Program ready (90-day Pro keys via Discord)

**Version Bump:**
- 0.19.x → 1.0.0 across all files (VERSION, main.py, package.json, Prometheus, health endpoint)
- Git tag: v1.0.0


## v0.19.0 (2026-02-08) — Analytics Redesign + Security Hardening

### Frontend
- **Analytics page redesign** — hero stat cards, gradient charts, fleet overview, model rankings with medal badges, animated printer utilization bars
- **Cost/Revenue time-series chart** — grouped bar chart tracking revenue vs cost over time
- **Dashboard MaintenanceWidget** — sidebar widget showing printers needing service with progress bars and overdue badges
- **Camera Picture-in-Picture** — draggable, resizable floating mini-player that persists while browsing
- **Jobs: due date + priority** — date picker and priority levels (low/normal/high/urgent) with color-coded badges
- **Jobs: RecentlyCompleted** — grid showing last 8 completed/failed prints with status and duration
- **Global search highlighting** — matched terms highlighted in search results
- **Dashboard layout fix** — MaintenanceWidget moved into sidebar (was orphaned outside grid)

### Backend
- **REST API documentation** — Swagger UI at `/api/docs`, ReDoc at `/api/redoc`
- **Rate limiting** — 10 login attempts per 5-minute window per IP (429 response)
- **Account lockout** — 5 failed attempts triggers 15-minute lockout (423 response)
- **Login attempt logging** — all attempts recorded to audit trail with IP address
- **Password complexity** — minimum 8 characters, requires uppercase + lowercase + number
- **Jobs schema** — added `due_date` and `priority` columns

### Session 2 — Polish & New Features
- **Printer Utilization Report** — dedicated /utilization page with fleet summary cards, utilization-by-printer bar chart, jobs/day chart, success/failure pie, per-printer breakdown table, CSV export
- **Light mode toggle** — Sun/Moon button in header, persists to localStorage, full CSS overrides for all farm-* classes
- **Models page stat cards** — total models, favorites, categories, costed count
- **Spools page stat cards** — total spools, total stock (grams), low stock count, material types
- **Orders page stat cards** — pending/in-progress/fulfilled/shipped counts, total revenue
- **Products page stat cards** — product count, component count, avg price, SKU coverage
- **EnergyWidget data wiring** — fetches last 200 jobs for real consumption data
- **PrusaLink + Elegoo monitors** — supervisord and systemd service files for process management
- **Printers page fix** — resolved useEffect/useQuery initialization order crash
- **Database migration** — added due_date and priority columns to jobs table
- **Audit log cleanup** — removed duplicate AuditLogViewer renders in Settings
- **ThemeToggle component** — self-contained theme switcher usable in both mobile and desktop headers

### Cleanup
- Removed 4 stale .bak files from frontend/src/pages/


## [0.18.1] - 2026-02-08
### Fixed
- Backend crash on `/api/models/{id}/mesh` — removed unresolvable ForeignKey constraints on `submitted_by`/`approved_by` columns
- `atPrinterLimit is not a function` crash on Printers page — replaced undefined function call with inline Community tier logic
- `failureModal is not defined` crash on Jobs page — moved state declaration from JobRow to Jobs component
- `handleDragEnd is not defined` crash on Jobs page — moved drag state and handlers from JobRow to Jobs component
- Printer cards flashing online/offline — WebSocket `last_seen` timestamp double-Z format fixed
- Filament slot display inconsistencies — hex colors no longer shown as names, casing normalized
- Removed non-functional 3D model viewer button — gcode .3mf exports don't contain mesh geometry

### Added
- Moonraker/Klipper: WebSocket push to frontend for real-time telemetry
- Moonraker/Klipper: MQTT republish support for Home Assistant/Grafana integration
- Moonraker/Klipper: Remaining time calculation (was hardcoded None)
- Moonraker/Klipper: Progress and layer data in WebSocket push

## [0.17.0] - 2026-02-07

### Added
- **Universal printer abstraction** - printer_events.py handles all monitors (Bambu, Moonraker, future Prusa/Elegoo)
- **Care counters** - Track total_print_hours, total_print_count, hours/prints_since_maintenance
- **HMS error alerts** - Bambu HMS codes parsed and dispatched as user alerts
- **Camera auto-discovery** - X1C and H2D cameras auto-populated from MQTT
- **Fail reason logging** - Automatic error recording when prints fail
- **Alerts system** - AlertBell component, Alerts page, dashboard widget, per-user preferences
- **Settings consolidation** - 7 tabs (General, Alerts, SMTP, SSO, Webhooks, Advanced, About), Simple/Advanced toggle
- **Fleet status widget** - Sidebar shows online printer count (●●●○○ 3/5)
- **Live heartbeat system** - 90-second threshold, last_seen tracking
- **Telemetry capture** - 10 columns: bed/nozzle temps+targets, gcode_state, print_stage, HMS, lights, nozzle type/diameter
- **Telemetry bottom bar** - Dashboard and Printer cards show live temps
- **Light toggle** - Bulb icon to control printer lights (with cooldown)
- **MQTT reconnection logic** - 30-second health checks, automatic reconnect
- **OIDC/SSO** - Microsoft Entra ID authentication with auto-user provisioning
- **Control Room Mode** - Full-screen camera grid with clock overlay (press F)
- **Camera enable/disable** - Toggle cameras without removing URLs
- **Browser Push Notifications** - VAPID-based push with service worker
- **Emergency Stop Button** - Floating button for stop/pause/resume active prints
- **One-click Print Again** - Clone completed jobs with RefreshCw button
- **Webhooks** - Discord/Slack integration with alert type filtering and test button
- **Email notifications** - SMTP-based alerts wired to dispatch_alert

### Changed
- SQLite now uses WAL mode for better concurrency
- Dashboard stats row reduced to 6 bubbles
- Mobile header layout fixed
- Printer cards show cleaner status display
- Maintenance system uses care counters for hour-based scheduling

### Fixed
- MQTT monitor stability with reconnection logic
- Kobra S1 camera streaming (MJPEG → ffmpeg → WebRTC)

## v0.15.0 (2026-02-04)

### New Features
- **Live progress bars** — Real-time progress + countdown timers on printer cards via MQTT
- **Job auto-linking by layer count** — Fingerprint matching when Bambu reports slicer profile instead of filename
- **Low spool warning** — Amber indicator on printer cards when any loaded spool < 100g
- **Quick print from upload** — "Schedule Now" button skips library, goes straight to scheduling
- **Favorites** — Star models to mark favorites, filter to show starred only
- **Model notes display** — Notes shown on model cards (edit modal already existed)
- **Global search** — Search bar in header (⌘K shortcut), searches models, jobs, spools, printers
- **Upload object checklist** — Detects objects on plate from .3mf, auto-unchecks wipe towers
- **Printer nicknames** — Optional friendly names shown on Dashboard and Printers page

### Bug Fixes
- Fixed MQTT double-encryption preventing printer connections
- Fixed job status case sensitivity (lowercase vs uppercase enum values)
- Fixed is_favorite field missing from models-with-pricing endpoint

### Technical
- Layer count extracted from .3mf and stored in print_files table
- MQTT monitor uses layer count as fallback matching strategy
- New `/api/search` endpoint for global search
- New `extract_objects_from_plate()` in threemf_parser



