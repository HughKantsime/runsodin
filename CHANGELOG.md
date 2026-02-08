# Changelog

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



