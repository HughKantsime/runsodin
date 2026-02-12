# O.D.I.N. — Complete Feature Set

> **Version:** v1.3.4 | **Last updated:** 2026-02-12
> **This document catalogs every feature in O.D.I.N. with implementation details and version introduced.**

---

## 1. Printer Monitoring & Control

### 1.1 Real-Time Telemetry
- Live nozzle temperature, bed temperature, fan speed via MQTT/WebSocket/REST polling
- Telemetry snapshots stored every 60 seconds during active prints (90-day retention)
- Telemetry timeseries chart on printer detail panel
- Fan speed extraction from MQTT heartbeat (Bambu), WebSocket (Moonraker), REST poll (PrusaLink), SDCP status (Elegoo)
- All four printer protocols at telemetry parity as of v1.1.4

### 1.2 Printer Status Tracking
- Real-time state detection: Idle, Printing, Paused, Error, Offline
- Live heartbeat system with 90-second threshold for online/offline determination
- Offline detection for all protocols (MQTT timeout for Bambu, WS disconnect for Moonraker, consecutive poll failures for PrusaLink, status timeout for Elegoo)
- Progress percentage and ETA displayed on printer cards and job views

### 1.3 HMS Error System (Bambu)
- 42 translated Bambu HMS error codes with human-readable descriptions
- HMS error history logging on change detection (90-day retention)
- HMS history panel on printer cards with toggle button
- Alerts dispatched on HMS errors

### 1.4 AMS Integration (Bambu)
- RFID spool detection — automatic filament type and color identification
- AMS slot display with loaded filament colors and material types
- AMS humidity and temperature monitoring (5-minute capture interval, 7-day retention)
- AMS environment chart (Recharts) on printer detail panel

### 1.5 Printer Controls
- **Emergency Stop** — floating red button visible on every page, sends stop to all connected printers (operator/admin)
- **Pause / Resume** — pause active prints, resume paused ones (operator/admin)
- **Light Toggle** — chamber light on/off for Bambu printers with cooldown (operator/admin)
- **Smart Plug Control** — on/off/status for configured smart plugs (operator/admin)

### 1.6 Smart Plug Integration
- Supported plug types: Tasmota (HTTP), MQTT, Home Assistant (REST API)
- Per-printer plug configuration in printer settings panel
- Auto power-on before job, auto power-off after completion with configurable cooldown
- Energy consumption tracking per job (kWh + cost)

### 1.7 Nozzle Lifecycle Tracking
- Track nozzle installs and retirements with timestamps
- Accumulated print hours per nozzle
- Nozzle status card on printer detail panel
- CRUD API endpoints for nozzle management

### 1.8 Printer Management UI
- Search, filter, and sort toolbar on Printers page (v1.1.0)
- Printer tags for categorization (v1.2.0)
- Printer card data panels with dedicated toolbar row: AMS / Telemetry / Nozzle / HMS toggle buttons
- Panels render inline within their card
- Camera URL field for P1S/A1 vision AI support (v1.1.0)
- Timelapse enable/disable toggle per printer (v1.2.0)

---

## 2. Camera System

### 2.1 Camera Feeds
- WebRTC streaming via go2rtc (RTSP → WebRTC transcoding)
- Camera grid page with all active feeds
- Per-camera enable/disable toggle

### 2.2 Camera Auto-Discovery
- Bambu X1C/H2D: RTSP URL broadcast via MQTT
- Moonraker/Klipper: webcam URL from Moonraker config
- PrusaLink: snapshot endpoint probed on connect
- Elegoo: MJPEG stream probed on port 8080
- MJPEG→WebRTC transcoding fix (ffmpeg:#video=h264 prefix for go2rtc)

### 2.3 Control Room Mode
- Full-screen multi-camera grid (press F on Cameras page)
- Exit with Escape

### 2.4 Picture-in-Picture
- Pop-out any camera feed into a draggable, resizable floating mini-player
- Persists while navigating to other pages

### 2.5 Camera Detail View (v1.1.3)
- Dedicated route: `/cameras/:id`
- Large WebRTC stream (~70% width) with printer data panels alongside
- Panels: status/temps/fan, filament slots with remaining weight, active job with progress bar + ETA
- AI indicator badge if vision is enabled
- Fullscreen button (native browser fullscreen)
- Back breadcrumb to Cameras grid
- Mobile: stacks vertically

### 2.6 TV Dashboard (v1.1.3)
- Dedicated route: `/tv` — full-viewport, no sidebar
- Opens in new tab from Cameras page
- Header: app branding, printing count, live clock + date
- Responsive grid of printer cards with WebRTC camera thumbnails, status, job progress + ETA
- Auto-pagination every 30 seconds for farms with 12+ printers, with page dots
- Stats bar with active alerts count
- Exit with Escape or X button

### 2.7 Timelapse Management (v1.2.0)
- Supervisord daemon (`timelapse_capture.py`) polls go2rtc every 30 seconds
- Captures JPEG frames for printers with `timelapse_enabled=True` while printing
- Stitches frames to MP4 via ffmpeg when print completes
- API: paginated list with printer/status filters, serve MP4 (supports `?token=` for `<video>` elements), delete (admin-only)
- Gallery page with grid view, video player modal, printer/status filters, pagination, download + delete actions
- Nav item with Film icon under Cameras

---

## 3. Vigil AI — Print Failure Detection

### 3.1 Detection Engine
- Supervised daemon (`vision_monitor.py`) managed by supervisord
- ONNX model inference for three failure types: spaghetti, first layer defects, detachment
- Per-printer monitoring threads that capture frames from go2rtc camera streams
- Confidence filtering with configurable thresholds per detection type
- Non-maximum suppression (NMS) for deduplication
- Confirmation buffer to reduce false positives — multiple consecutive detections required before alerting
- Auto-pause capability: automatically pauses prints when failures are confirmed

### 3.2 Detection Management
- Detections page with grid/list view of all detected failures
- Filter by printer, detection type, and review status (pending/confirmed/dismissed/auto_paused)
- Frame viewer showing captured image with detection overlay
- Review workflow: confirm, dismiss, or label detections for training data

### 3.3 Vigil API (15 endpoints)
- `GET/PATCH /api/vision/detections` — list and review detections
- `GET/PATCH /api/printers/{id}/vision` — per-printer vision settings (enable/disable, sensitivity)
- `GET/PATCH /api/vision/settings` — global vision configuration
- `GET/POST /api/vision/models` — manage ONNX model files (upload new, activate)
- `GET /api/vision/stats` — detection statistics and trends
- Training data endpoints: export labeled frames, label detections for model retraining

### 3.4 Storage
- Detection frames stored at `/data/vision_frames/{printer_id}/`
- ONNX models stored at `/data/vision_models/`
- Default models copied from `backend/vision_models_default/` on first boot
- Models tracked via Git LFS (files >100 MB)

---

## 4. Job Management

### 4.1 Job Lifecycle
- States: Pending → Printing → Completed/Failed
- With approval mode: Submitted → Approved → Pending → Printing → Completed/Failed
- Rejected jobs can be resubmitted by the original submitter

### 4.2 Scheduling
- Smart scheduler with color-match scoring to minimize filament swaps
- Upload → Model Library → Schedule → Jobs workflow pipeline
- Quick Print button on upload success screen — creates pending job immediately (v1.0.26)
- Quick-schedule presets for common configurations (v1.2.0)
- Due dates and priority levels: low / normal / high / urgent with color badges

### 4.3 Queue Management
- Drag-and-drop queue reorder (operator/admin)
- Three tabs: All, Order Jobs, Ad-hoc
- At-risk job warnings — overdue jobs show red tint background (v1.0.26)
- Job edit modal for inline editing (v1.1.0)
- Bulk operations with multi-select checkboxes: cancel, priority change, reschedule, delete (v1.3.0)

### 4.4 Print Again
- One-click clone of any completed or failed job
- Creates new pending job with same model and printer assignment

### 4.5 Failure Logging
- Failure reason dropdown (nozzle clog, adhesion failure, filament tangle, etc.) + free-text notes
- Failure analytics with correlation data (v1.2.0) — which printer fails most, which model, patterns

### 4.6 Job Approval Workflow (Education)
- Toggle "Require Job Approval" in Settings (admin-only)
- Viewers (students) submit jobs → Submitted status
- Awaiting Approval tab with badge count visible to operators/admins
- Approve/Reject buttons with notification dispatch
- Rejected jobs: student can edit and Resubmit
- Operators/admins bypass approval — their jobs go directly to Pending
- Targeted approval alerts routed to group owner (v1.1.0)

### 4.7 Per-Job Time Tracking (v1.2.0)
- Actual print duration tracked alongside slicer estimate
- Time data visible on job detail and available in exports

### 4.8 Timeline View
- Gantt-style view of jobs across all printers
- Time on horizontal axis for fleet utilization visualization

### 4.9 Recently Completed
- Grid showing last 8 completed/failed prints with thumbnails, print times, outcomes

---

## 5. Model Library

### 5.1 Upload & Metadata
- .3mf file upload with automatic metadata extraction: print time estimate, filament usage (grams per material), target printer model, thumbnail
- Object checklist — individual objects in the build plate with checkboxes
- Wipe tower auto-unchecked
- `quantity_per_bed` calculated from checked objects and persisted

### 5.2 Library Management
- Search by name, material, or tags
- Favorites (star frequently-used models)
- Stat cards: total models, total print hours, most-printed
- Schedule a job, edit metadata, delete from any model

### 5.3 Model Versioning (v1.3.0)
- Revision history panel per model (accessible via History button on model cards)
- Upload new .3mf revisions with changelog notes
- Browse revision history with version numbers, dates, and uploader

---

## 6. Filament & Spool Inventory

### 6.1 Spool Management
- Track material type, color, brand, batch, starting weight, current weight remaining, cost per gram, status (active/empty/archived)
- Stat cards: total spools, total weight remaining, low-stock warnings
- Create/edit/delete spools (operator/admin)

### 6.2 QR Label System
- Unique QR code per spool
- Print individual labels or batch labels
- Scan via camera (HTTPS required), USB barcode scanner, or manual entry
- Scanning opens dialog to associate spool with printer + AMS slot

### 6.3 Filament Drying Log (v1.2.0)
- Log drying sessions per spool: start time, duration, temperature
- Track filament condition over time

### 6.4 Pricing Configuration
- Per-material cost rates (cost per gram for PLA, PETG, ASA, etc.)
- Feeds into cost calculator and order P&L calculations
- Admin-only configuration under Settings → Pricing

---

## 7. Consumables (v1.0.33)

- Non-printed supplies: screws, magnets, rubber feet, packaging, labels, glue, etc.
- Full CRUD page with stock levels, low-stock alerts, search/filter
- Product BOM includes consumables alongside printed components
- Auto-deduct consumable stock when orders are scheduled
- Consumable costs included in COGS calculation

---

## 8. Products & BOM

### 8.1 Product Catalog
- Name, SKU, sale price, description
- Stat cards on Products page
- Create/edit/delete (operator/admin; delete admin-only)

### 8.2 Bill of Materials
- Two component types: Printed (models from library) and Consumables
- Quantity per product for each component
- COGS auto-calculated from filament usage + consumable costs

---

## 9. Orders & Fulfillment

### 9.1 Order Management
- Platform source (Etsy, Amazon, wholesale, direct)
- Customer name, order reference, line items referencing Products
- Order edit modal for inline editing (v1.1.0)
- Stat cards: total orders, revenue, pending orders, average margin

### 9.2 Order Lifecycle
- Pending → In Progress (Schedule All) → Fulfilled (all jobs complete) → Shipped (with tracking number)
- "Schedule All" auto-generates jobs from BOM, assigns to printers with color-match scoring, deducts consumable stock

### 9.3 Per-Order P&L
- Revenue: sale price × quantity per line item
- COGS: filament cost + consumable cost for all BOM components
- Profit and margin calculated per order

### 9.4 PDF Invoice Export (v1.0.26)
- Branded PDF invoices via `GET /api/orders/{id}/invoice.pdf` using fpdf2
- Integrates branding system: logo, colors, app name
- Download Invoice button on order detail modal

### 9.5 Shipping
- Mark Shipped with tracking number (operator/admin)

---

## 10. Analytics & Reporting

### 10.1 Analytics Dashboard
- Hero stat cards: total revenue, total cost, profit margin, jobs completed
- Cost/revenue time-series bar chart
- Model rankings by job count
- Fleet overview

### 10.2 Cost Calculator
- Detailed per-model cost breakdown: material, labor, electricity, depreciation, packaging, overhead, failure buffer
- Configurable manufacturing margin
- Per-unit and bulk quantity pricing calculations
- Dedicated Calculator page

### 10.3 Printer Utilization Report
- Fleet summary: average utilization, total print hours, busiest printer
- Per-printer breakdown: utilization percentage, total hours, job count
- Chart and table views
- CSV export

### 10.4 Energy Tracking
- Per-job energy consumption (kWh) and cost from smart plug data
- EnergyWidget in Analytics dashboard with consumption trends

### 10.5 Failure Analytics (v1.2.0)
- Failure reason correlation: which printer fails most, which model, pattern detection
- Built on failure logging data

### 10.6 CSV Exports
- Jobs, models, spools, filament usage, audit logs (admin-only)
- Available from relevant pages and Settings → Export

### 10.7 Cost Chargebacks (v1.3.0)
- Auto-links `charged_to_user_id` on job creation
- Chargeback report with date filters and per-user breakdown (jobs, hours, grams, cost)
- Report UI in Settings → System tab

### 10.8 Education Usage Reports
- Per-user job metrics: job count, hours, cost, accuracy
- Configurable time window (7–90 days)
- Aggregates stats by printer and model
- API endpoint gated by Education license tier

### 10.9 Report Schedule Definitions (v1.3.0)
- CRUD for report schedule records: name, type, frequency (daily/weekly/monthly), recipients
- Supported types: fleet utilization, job summary, filament consumption, failure analysis, chargeback summary
- Toggle active/inactive, manage recipients
- Report schedule management UI in Settings → System tab
- *Note: Schedule definitions only — automated report generation and delivery is not yet implemented*

---

## 11. Multi-User & Access Control

### 11.1 Authentication
- JWT-based authentication with Bearer tokens
- OIDC/SSO via Microsoft Entra ID with auto-user provisioning
- Unified JWT signing for local auth and OIDC

### 11.2 TOTP MFA (v1.3.0)
- QR code setup flow via MFASetup component
- 6-digit TOTP challenge on login
- Admin can force-enable or disable MFA per user

### 11.3 RBAC (Role-Based Access Control)
- Three roles: admin, operator, viewer
- Enforced on all API endpoints (164+ as of v1.0.0, expanded since)
- Viewer: read-only + submit jobs (with approval mode)
- Operator: day-to-day production (create/edit jobs, spools, models, orders, products, maintenance)
- Admin: full system control (users, settings, branding, SSO, webhooks, backups, license, pricing)

### 11.4 User Groups (v1.1.0)
- Group management UI for organizing users
- Group owner concept for targeted approval alert routing

### 11.5 Organizations (v1.3.0)
- Create organizations to group users and assign printer/model/spool resources
- Member management (add/remove users) and printer assignment within organizations
- Organization management UI in Settings → Access tab

### 11.6 Scoped API Tokens (v1.3.0)
- `odin_` prefixed tokens with granular per-route scopes
- Scopes: read:printers, write:jobs, etc.
- APITokenManager component for CRUD

### 11.7 Session Management (v1.3.0)
- JWT `jti` tracking for session identification
- Active sessions list with device info
- Revoke individual sessions or all sessions
- Token blacklist for immediate invalidation

### 11.8 Print Quotas (v1.3.0)
- Per-user limits: jobs, grams, hours
- Configurable period: daily, weekly, monthly, semester
- Enforced at job creation
- QuotaManager component for configuration

---

## 12. Security

### 12.1 Authentication Security
- Rate limiting: 10 login attempts per 5-minute window per IP
- Account lockout: 5 failed attempts → 15-minute lock (in-memory, resets on restart)
- Password complexity: minimum 8 characters, uppercase + lowercase + number
- Login attempt audit trail with IP addresses

### 12.2 IP Allowlisting (v1.3.0)
- CIDR-based middleware enforcement
- Lock-out protection (can't lock yourself out)
- IPAllowlistSettings component

### 12.3 Data Protection
- Fernet encryption for printer credentials at rest
- Ed25519 license key signing (production keys deployed, dev bypass removed)
- SQL injection prevention: parameterized queries + column whitelist on user updates
- Gitleaks pre-commit hook for secret scanning

### 12.4 GDPR Compliance (v1.3.0)
- Full JSON export of all user data
- Anonymization endpoint that preserves analytics while removing PII

### 12.5 Data Retention Policies (v1.3.0)
- Configurable TTL per data type
- Manual cleanup trigger
- DataRetentionSettings component

### 12.6 Auth Bypass Prevention
- `/label` endpoints: exact path matching (not substring)
- Branding: only GET unauthenticated; PUT/POST/DELETE require admin
- Setup endpoints: permanently locked after setup completion

### 12.7 WCAG 2.1 Accessibility (v1.3.0)
- Skip-to-content link
- Landmark roles on major page sections
- `aria-label` on 50+ icon-only buttons
- `role="dialog"` on 17 modals
- Form label associations
- Table `scope="col"` headers
- Progressbar roles on progress indicators

---

## 13. Notifications

### 13.1 Channels
- **Browser Push** — VAPID-based, no external service required
- **Email** — SMTP configuration
- **Webhooks** — Discord/Slack integration with configurable events
- **ntfy** — lightweight push via HTTP POST
- **Telegram** — Bot API integration

### 13.2 Quiet Hours
- Configurable time window for notification suppression
- External notifications (email, push, webhooks) suppressed during quiet hours
- Alerts still saved to database during suppression
- Digest formatting functions exist for batching suppressed alerts (delivery mechanism not yet automated)

### 13.3 In-App Alerts
- Alert bell icon with unread count in header
- Filterable Alerts page
- Mark individual alerts as read
- Delete alerts (operator/admin)
- Targeted approval alerts routed to group owners (v1.1.0)

---

## 14. Integrations

### 14.1 MQTT Republish
- Forward printer events to configurable external MQTT broker
- Topics: printer status, print lifecycle, AMS changes, queue events, maintenance alerts
- Use with Home Assistant, Node-RED, Ignition

### 14.2 Prometheus Metrics
- `/metrics` endpoint in Prometheus format
- Printer telemetry, job counts, system health
- Grafana-ready

### 14.3 WebSocket
- `/ws` endpoint for real-time push to frontend
- Replaces polling for live updates

### 14.4 REST API Documentation
- Swagger UI at `/api/docs`
- ReDoc at `/api/redoc`

---

## 15. White-Label Branding

- Custom application name and subtitle
- Primary and accent color customization
- Logo upload
- 64 web fonts selectable for UI (v1.1.0)
- Branding applied across UI and PDF invoices
- CSS custom properties approach

---

## 16. UX & Interface

### 16.1 Themes
- Light/dark mode toggle (sun/moon icon in header)
- Persisted to localStorage

### 16.2 Navigation
- Sidebar with all page links; collapses to hamburger on mobile
- Fleet status widget in sidebar (printer counts by state)
- Global search (Cmd+K or /) with result highlighting across printers, jobs, models, spools, orders, products

### 16.3 Keyboard Shortcuts
- `?` — help modal
- `/` — focus search
- `N` — new item (context-dependent)
- `G then D/J/P` — navigate to Dashboard/Jobs/Printers
- `F` — Control Room mode (Cameras page)
- `Escape` — close modal/exit mode

### 16.4 PWA
- Progressive Web App with manifest and service worker
- Add-to-homescreen, standalone mode
- Installable web app (v1.2.0)

### 16.5 Internationalization
- Multi-language: EN, DE, JA, ES (181 keys)

### 16.6 Dashboard Layout
- Full-width stacked printer card layout (v1.1.0)
- Maintenance alerts widget with progress bars and overdue badges

### 16.7 Settings Consolidation
- 7 tabs (consolidated from 12 in v1.1.0)
- Simple/Advanced view toggle
- Sub-sections: General, Access, Notifications, Integrations, Branding, Vigil, System

---

## 17. Maintenance

- Scheduled maintenance tasks per printer with due dates and recurring intervals
- Complete tasks to reset counters; recurring tasks auto-create next instance
- Dashboard widget showing upcoming and overdue tasks
- Care counters: total print hours, prints since last maintenance

---

## 18. Backup & Restore

- Create point-in-time database snapshots (admin-only)
- Download backup files
- Backup restore via UI (v1.3.0) — upload .db file through restore panel, auto-safety-backup before overwrite
- Uses SQLite `.backup` API (not file copy) during WAL mode

---

## 19. Audit & Compliance

### 19.1 Audit Log
- Searchable history of all admin actions with timestamps, user, IP address
- Audit log viewer in Settings (admin-only)
- CSV export with filters (v1.0.26)
- Full audit log system (v1.2.0)

### 19.2 Bulk Operations (v1.3.0)
- Jobs: multi-select checkboxes with bulk cancel, priority change, reschedule, delete
- Printers: bulk enable, disable, add tag (API-level)

---

## 20. License System

### 20.1 License Tiers
- Community (free, 5 printers, 1 user)
- Pro (unlimited printers/users, all production features)
- Education (Pro + job approval + usage reports + quotas)
- Enterprise (Pro + MFA + IP allowlist + organizations + GDPR + compliance)

### 20.2 License Enforcement
- Ed25519 signed license files, verified locally (no phone home)
- Air-gap friendly — works without network after install
- Frontend gating via ProGate component + LicenseContext
- License upload via Settings UI (admin-only)
- BSL 1.1 license — converts to Apache 2.0 after 3 years per version

### 20.3 License Manager
- Separate Flask web app on dedicated server (192.168.70.6)
- Issue, revoke, reissue, verify, download license files
- Full audit trail
- Ed25519 signing compatible with ODIN's odin-license-v1 format

---

## 21. Supported Printer Protocols

### 21.1 Bambu MQTT
- Printers: X1C, A1, P1S, H2D
- Connection: MQTT over TLS (port 8883), protocol v3.1.1
- Receives: temps, progress, state, AMS RFID, HMS errors, camera URL, fan speed
- Credentials: serial + access code, Fernet-encrypted at rest

### 21.2 Moonraker REST/WebSocket
- Printers: Any Klipper printer (Anycubic Kobra S1, Creality K1, Voron, QIDI, Sovol)
- Connection: REST API + WebSocket
- Receives: temps, progress, state, file list, webcam URL

### 21.3 PrusaLink REST
- Printers: MK4/S, MK3.9, MK3.5, MINI+, XL, CORE One
- Connection: REST polling
- Receives: temps, progress, state, camera snapshot, fan speed (v1.1.4)
- Error recording to HMS error history (v1.1.4)
- Offline detection after 5 consecutive poll failures (v1.1.4)

### 21.4 Elegoo SDCP
- Printers: Centauri Carbon, Neptune 4, Saturn (resin)
- Connection: SDCP WebSocket protocol
- Receives: temps, progress, state, fan speed (v1.1.4), enclosure temperature (v1.1.4)
- Print failure detection on state transition (v1.1.4)
- Offline detection after 60-second timeout (v1.1.4)

---

## 22. Deployment

### 22.1 Docker
- Single container with supervisord managing all 7 processes
- `docker compose up` deployment
- Volume mount for persistent data (database, backups, uploads, logs, secrets, timelapses)
- Auto-generated secrets on first run
- Onboarding wizard on first launch (5 steps: Welcome → Admin → Name → Network → Printer)
- Setup endpoints permanently locked after completion

### 22.2 GHCR Distribution
- Images published to `ghcr.io/hughkantsime/odin`
- Version-tagged (e.g., `v1.3.0`)
- CI/CD via GitHub Actions — triggered on git tags only

### 22.3 Database Migrations
- Idempotent column additions via `entrypoint.sh`
- Tables: api_tokens, active_sessions, token_blacklist, quota_usage, model_revisions, report_schedules, timelapses (v1.2.0–v1.3.0)
- Columns: MFA fields on users, quota fields on users, chargeback columns on jobs, org columns on groups/printers/models/spools, timelapse_enabled on printers

---

## Feature Introduction Timeline

| Version | Date | Features Added |
|---|---|---|
| v0.1.0–v0.6.0 | 2026-01-27–30 | Core: dashboard, printers, jobs, models, spools, .3mf upload |
| v0.7.0–v0.9.x | 2026-01-30–02-02 | Auth, JWT, cameras, RBAC, branding, mobile responsive |
| v0.11.0 | 2026-02-02 | Maintenance, permissions matrix, workflow pipeline |
| v0.13.0–v0.14.0 | 2026-02-03 | Pricing, analytics, CSV export, orders, products, BOM |
| v0.15.0 | 2026-02-04 | Dashboard progress bars, MQTT job auto-linking, search, favorites |
| v0.16.0 | 2026-02-05 | Moonraker/Klipper integration, QR scanner |
| v0.17.0 | 2026-02-07 | Universal printer abstraction, alerts, SSO, Control Room, push, webhooks, email, e-stop |
| v0.18.0 | 2026-02-08 | License gating, MQTT republish, Prometheus, WebSocket, i18n, PWA, smart plugs, keyboard shortcuts, quiet hours, HMS decoder, drag-drop queue, failure logging |
| v0.19.0 | 2026-02-09 | Analytics redesign, utilization report, light mode, PrusaLink/Elegoo adapters, stat cards, PiP, due dates, priorities |
| v1.0.0 | 2026-02-09 | Security audit (53 findings), RBAC on all 164 endpoints, Ed25519 production keys |
| v1.0.26 | 2026-02-10 | PDF invoices, at-risk warnings, audit export, Quick Print, gitleaks, 35 unit tests |
| v1.0.33 | 2026-02-10 | Consumables, telemetry snapshots, fan speed, HMS history, nozzle lifecycle |
| v1.0.32 | 2026-02-10 | Camera auto-discovery for all printer types |
| v1.1.0 | 2026-02-11 | User groups, 64 web fonts, printer toolbar, job/order edit modals, Settings consolidation, dashboard layout, targeted approvals |
| v1.1.3 | 2026-02-11 | Camera Detail View, TV Dashboard |
| v1.1.4 | 2026-02-11 | Elegoo + PrusaLink telemetry parity (fan, snapshots, pruning, offline detection) |
| v1.2.0 | 2026-02-12 | Timelapse management, audit log, PWA install, per-job time tracking, failure analytics, printer tags, filament drying log, quick-schedule presets |
| v1.3.0 | 2026-02-12 | TOTP MFA, scoped API tokens, session management, IP allowlist, organizations, print quotas, cost chargebacks, GDPR, data retention, scheduled reports (definitions), bulk operations, backup restore UI, model versioning, WCAG 2.1, Vigil AI failure detection |
| v1.3.1–1.3.4 | 2026-02-12 | Bugfixes: vision_monitor crash-loop prevention, numpy<2 pin for ONNX compatibility, SQLAlchemy connection pool leak in middleware, "Vision AI" → "Vigil AI" rename |
