# PrintFarm Scheduler Changelog

## v0.8.2 - Role-Based UI Permissions (2026-01-31)

### RBAC UI Enforcement
- New permissions.js config: centralized role-based access control
- Nav items hidden based on role (viewer cannot see Upload, Settings, Admin)
- Action buttons hidden per role across all pages
- JWT decoded client-side for role detection

### Per-Page Controls
- Dashboard: Run Scheduler, job start/complete/cancel buttons (operator+)
- Jobs: New Job, cancel, delete buttons (operator+)
- Printers: Add Printer (admin only), edit/delete, Sync AMS, slot editing (operator+)
- Models: Add Model, edit (operator+), delete (admin only)
- Spools: Add Spool, load/unload, record usage (operator+), archive (admin only)

### Permission Matrix
- Viewer: read-only access to all pages, no action buttons
- Operator: full day-to-day operations (create, edit, schedule)
- Admin: full access including add/delete printers, delete models/spools, settings, user management


## v0.8.1 - Route Protection & Auth Enforcement (2026-01-31)

### Frontend Auth
- ProtectedRoute wrapper checks JWT expiry before rendering any page
- Expired or missing tokens redirect to /login automatically
- Login page renders without sidebar (clean standalone view)
- JWT token included on all API requests alongside API key
- 401 responses auto-clear stored token and redirect to login

### Security
- No pages accessible without valid JWT (except /login)
- Token expiry validated client-side via decoded JWT payload
- Consistent auth headers on all fetchAPI calls


## v0.8.0 - Camera Feeds & UI Polish (2026-01-31)

### Camera Infrastructure
- go2rtc integration for RTSP → WebRTC stream conversion
- Auto-detect Bambu printer cameras from encrypted credentials (no manual URL config)
- WebRTC signaling proxy through backend with API key authentication
- go2rtc config auto-generated and synced from database
- Streams use RTSPS on port 322 with per-printer access codes

### Cameras Page (/cameras)
- Grid view of all active camera feeds
- Configurable layout: 1, 2, or 3 column grid
- Live/connecting/error status indicators
- Expand button for full-size modal overlay

### Camera Modal
- Quick-look video overlay from any page
- WebRTC streaming with automatic connection management
- Status indicator and printer name header

### UI Polish
- Collapsible sidebar with icons-only mode (smooth transition)
- Color name resolver for Bambu filament names (e.g., "Caramel Matte" → #C68E5B)
- Circular color swatches on Jobs page (replacing pill badges)
- Duration formatting: "2h 15m" instead of "0.45h"
- Camera icon on dashboard printer cards for printers with feeds

### Backend
- `camera_url` field on printers table with migration
- `get_camera_url()` helper with auto-generation from Bambu credentials
- `sync_go2rtc_config()` for dynamic stream management
- `/api/cameras` - list printers with available feeds
- `/api/cameras/{id}/stream` - get stream info and sync go2rtc
- `/api/cameras/{id}/webrtc` - WebRTC signaling proxy

## v0.7.0 - User Management & Authentication (2026-01-30)

### Authentication System
- JWT-based authentication with 24-hour token expiry
- Password hashing with bcrypt
- Role-based access control (admin/operator/viewer)
- Logout button in sidebar

### Login Page (/login)
- Clean login form with PrintFarm branding
- Token stored in localStorage
- Redirects to dashboard on success

### Admin Page (/admin)
- User management for administrators
- Create, edit, delete users
- Assign roles and enable/disable accounts
- View last login timestamps

### Database
- New `users` table with migration
- Fields: username, email, password_hash, role, is_active, last_login

### API Endpoints
- `POST /api/auth/login` - Authenticate and get JWT token
- `GET /api/auth/me` - Get current user info
- `GET /api/users` - List users (admin only)
- `POST /api/users` - Create user (admin only)
- `PATCH /api/users/{id}` - Update user (admin only)
- `DELETE /api/users/{id}` - Delete user (admin only)

### Other
- Delete uploaded files endpoint
- Cancel job endpoint

### Security Notes
- Auth is optional - existing API key still works
- Admin page requires login + admin role
- Set JWT_SECRET_KEY env var in production

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
- Dashboard auto-fit card heights
- Always use 4 columns for filament slots
- Simplified status display with green/red dots
- Printers page slot display matches dashboard style
- Needs-attention warnings fixed for RFID-matched spools

## v0.5.0 - RFID Auto-Tracking (2026-01-28)
- RFID spool detection from AMS
- Auto-create spools and library entries from unknown RFID tags
- Per-spool color tracking
- Weight sync from AMS percentage
- Support filament detection (PLA-S, PA-CF, etc.)
- Tracked spool assignment in slot editor
- Needs-attention indicator for unassigned slots
- Deduplicate library entries by brand+name+material

## v0.4.0 - Spool Management and Audit Logging (2026-01-28)
- Spools page with sorting and grouping
- Spool weight tracking
- Audit logging system
- Label endpoint authentication bypass

## v0.3.0 - Analytics and Calculator (2026-01-28)
- Analytics dashboard with revenue tracking
- Value-per-hour calculations
- Print cost calculator
- Per-printer performance metrics

## v0.2.0 - Printers and Timeline (2026-01-27)
- Printers management page
- Filament slot editor
- Timeline/Gantt view
- Drag-and-drop job scheduling
- Spoolman integration

## v0.1.0 - Initial Release (2026-01-27)
- Core scheduling engine with color-match scoring
- Job queue management
- Printer and filament state tracking
- React frontend with dashboard
- FastAPI backend with SQLite
- Docker support
