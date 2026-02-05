# Changelog

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

