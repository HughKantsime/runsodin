# PrintFarm Scheduler Changelog

## v0.6.0 - 3MF Upload and Auto-Schedule (2026-01-30)

### New Feature: .3mf File Upload
- Parse sliced Bambu Studio .3mf files
- Extract metadata: print time, weight, layers, filaments, thumbnail
- Store in print_files database table
- Preview with thumbnail before scheduling

### Upload Page (/upload)
- Drag-and-drop file upload
- Preview extracted metadata and thumbnail
- Select printer or auto-assign
- One-click schedule to create job
- Recent uploads list

### API Endpoints
- `POST /api/print-files/upload` - Upload and parse .3mf
- `GET /api/print-files` - List uploaded files
- `GET /api/print-files/{id}` - Get file details
- `DELETE /api/print-files/{id}` - Delete file
- `POST /api/print-files/{id}/schedule` - Create job from file

### Dashboard Improvements
- Active jobs show printer name correctly
- Duration formatted as "27m" instead of raw decimal
- Color swatches instead of hex text

## v0.5.2 - MQTT Print Job Tracking (2026-01-30)

### MQTT Print Tracking
- New `mqtt_monitor.py` daemon connects to all Bambu printers via MQTT
- Auto-tracks print jobs: start time, end time, duration, layers, status
- Runs as systemd service (`printfarm-monitor`)
- New `print_jobs` database table with migration

### API Enhancements
- `GET /api/print-jobs` - List print history with filters
- `GET /api/print-jobs/stats` - Per-printer hours and job counts
- `POST /api/printers/reorder` - Persist printer display order
- `/api/stats` now includes MQTT running/completed jobs
- `/api/timeline` includes MQTT-tracked jobs

### Dashboard Updates
- Active Jobs section shows running MQTT prints
- Recent Prints shows completed/failed jobs only
- Printer cards display tracked hours and job count
- Currently Printing stat includes MQTT jobs
- Completed Today stat includes MQTT jobs

### Timeline
- MQTT-tracked prints appear on timeline
- Removed cluttered color list from printer sidebar

### Printers Page
- Drag-drop reorder now persists to database
- All views respect display_order

## v0.5.1 - UI Polish (2026-01-29)
- Dashboard 9-slot layout for H2D
- Needs-attention warnings fixed for RFID-matched spools
- Slot editor improvements

## v0.5.0 - RFID Auto-Tracking (2026-01-28)
- RFID spool detection from AMS
- Auto-create spools and library entries
- Per-spool color tracking
- Weight sync from AMS percentage
- Support filament detection (PLA-S, PA-CF, etc.)
