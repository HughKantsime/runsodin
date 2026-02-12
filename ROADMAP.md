# O.D.I.N. Feature Roadmap

Generated 2026-02-12 at v1.1.4.

## Backlog (prioritized)

### 1. Fleet Failure Analytics Dashboard
**Effort: Medium** — Data already exists in jobs, print_jobs, hms_error_history tables.

Add failure analytics section to Analytics page:
- Success/failure rate by printer (bar chart)
- Success rate by model (which models fail most)
- Failure rate by filament type
- Mean time between failures per printer
- Most common failure reasons (from failure_reason field)
- HMS error frequency by code

Backend: `GET /api/analytics/failures` with date range filters.
Frontend: charts on Analytics page.

---

### 2. Printer Tags/Groups for Fleet Organization
**Effort: Medium** — Structural improvement that unlocks smarter scheduling.

Free-form labels on printers: "Room A", "PLA-only", "Production", "Testing".

Backend: `printer_tags` table (printer_id, tag), endpoints to add/remove/filter. Tags usable as scheduler constraints.
Frontend: tag chips on printer cards, tag filter on Printers page, tag editor in printer settings, optional tag constraint on job creation.

---

### 3. Audit Log Viewer Page
**Effort: Low** — Table already exists, CSV export works, just needs a JSON endpoint + page.

Admin-only page at `/audit`. Filterable table: timestamp, user, action, target, details. Filters: date range, user, action type. Paginated (50/page).

Backend: `GET /api/audit-logs` with pagination + filters (JSON, not CSV).
Frontend: new `AuditLog.jsx` page.

---

### 4. PWA Manifest for Mobile Install
**Effort: Low** — Push notifications already work.

- `manifest.json` (app name from branding, icons, theme color, `display: standalone`)
- Basic service worker for app shell caching (not offline-first data)
- Meta tags in `index.html` (theme-color, apple-mobile-web-app-capable)
- Install prompt or button in Settings

Goal: "Add to Home Screen" on tablets carried around the farm floor.

---

### 5. Estimated vs Actual Print Time Tracking
**Effort: Low** — Data already exists, just needs columns + chart.

Add `estimated_duration_min` and `actual_duration_min` to jobs table. Estimated from slicer metadata at creation, actual from print completion events.

Backend: `GET /api/analytics/time-accuracy` with per-printer accuracy stats.
Frontend: est vs actual on job cards, accuracy trend chart on Analytics page.

---

### 6. Filament Drying Log
**Effort: Medium** — New table + UI. Important for production farms with hygroscopic materials.

Track drying sessions per spool (nylon, PETG, TPU, PA).

Backend: `drying_logs` table (spool_id, dried_at, duration_hours, temp_c, method, notes). `POST /api/spools/{id}/dry`, `GET /api/spools/{id}/drying-history`. Alert when hygroscopic spool overdue for drying.
Frontend: "Log Drying" button on spool detail, drying history, "needs drying" indicator on spool cards.

---

### 7. Print Profiles / Presets
**Effort: Medium** — Quality-of-life for operators doing repeat work.

Save reusable configs: model + preferred printer/tag + filament + quantity + notes.

Backend: `print_presets` table, CRUD endpoints, `POST /api/presets/{id}/schedule`.
Frontend: Presets section on Jobs page, "Save as Preset" on job form, "Quick Schedule" from presets.

---

### 8. Timelapse Generation from Camera Streams
**Effort: High** — Needs ffmpeg in Docker, background capture thread, storage management.

Capture JPEG snapshots every 30s during prints via existing camera endpoint. On completion, stitch with ffmpeg into MP4. Store in `/data/timelapses/`.

Backend: timelapse capture service, `GET /api/jobs/{id}/timelapse`, `GET /api/timelapses`, `DELETE /api/timelapses/{id}`. `timelapse_enabled` flag per printer. Auto-delete after N days.
Frontend: timelapse player on job detail, gallery page, toggle in printer settings.

---

## Parked (needs more design)

### Auto-Queue / Auto-Start Next Job
After print completes, automatically send next queued job to the printer. Requires file sending to printers, which requires guardrails:
- Extract printer profile from uploaded 3MF/gcode metadata
- Tag files with compatible printer type(s)
- Enforce compatibility at schedule time
- Final bed-size validation before send

Bambu (3MF over MQTT) is inherently safe — printer rejects incompatible files. Klipper/PrusaLink (raw gcode) is the risky case — needs metadata extraction + validation.
