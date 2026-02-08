#!/bin/bash
# v0.18.1 Bugfix Release — Run on server after 3pm
# ================================================

set -e
cd /opt/printfarm-scheduler

# 1. Update VERSION
echo "0.18.1" > VERSION

# 2. Update CHANGELOG
cat > CHANGELOG_v0181_entry.txt << 'ENTRY'

### v0.18.1 (2026-02-08) — Bugfix Release

**Backend Fixes:**
- Fixed crash on `/api/models/{id}/mesh` — removed unresolvable ForeignKey constraints on `submitted_by`/`approved_by` columns in models.py (User model defined in main.py, not models.py)

**Frontend Fixes:**
- Fixed `atPrinterLimit is not a function` crash on Printers page — replaced undefined function call with inline Community tier logic
- Fixed `failureModal is not defined` crash on Jobs page — moved state declaration from JobRow to Jobs component
- Fixed `handleDragEnd is not defined` crash on Jobs page — moved drag state and handlers from JobRow to Jobs component
- Fixed printer cards flashing online/offline — WebSocket `last_seen` timestamp had double-Z format (`2026-02-08T20:26:09.123ZZ`), now matches backend SQLite format
- Fixed filament slot display inconsistencies — hex colors no longer shown as names, casing normalized between Dashboard and Printers pages
- Removed non-functional 3D model viewer button — gcode .3mf exports don't contain mesh geometry

**Moonraker/Klipper Parity:**
- Added WebSocket push to frontend for Klipper printer telemetry (real-time card updates)
- Added MQTT republish support for Klipper printers (Home Assistant/Grafana integration)
- Added remaining time calculation for Klipper prints (was hardcoded None)
- Added progress and layer data to WebSocket push for Klipper printers
ENTRY

echo ""
echo "=== CHANGELOG entry to prepend ==="
cat CHANGELOG_v0181_entry.txt
echo ""
echo "=== Manually prepend the above to CHANGELOG.md, then continue ==="
echo ""

# 3. Rebuild frontend
cd frontend
npm run build
cd ..

# 4. Restart services
systemctl restart printfarm-backend
systemctl restart printfarm-monitor

# 5. Verify
echo ""
echo "=== Verify ==="
echo "VERSION: $(cat VERSION)"
echo "Backend: $(curl -s http://localhost:8000/api/health 2>/dev/null | head -c 100 || echo 'checking...')"
sleep 2
echo "Frontend loads: $(curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/)"
echo ""

# 6. Git commit (ONLY after 3pm!)
echo "=== Ready to commit ==="
echo "Run these commands after 3pm:"
echo ""
echo "cd /opt/printfarm-scheduler"
echo "git add -A"
echo "git status"
echo 'git commit -m "v0.18.1: bugfix release — frontend crashes, WebSocket timestamp, Moonraker parity"'
echo "git tag v0.18.1"
echo "git push origin master --tags"
