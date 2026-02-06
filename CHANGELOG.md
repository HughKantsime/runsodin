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


## [0.16.0] - 2026-02-05

### Added
- **Moonraker/Klipper integration** - Support for Kobra S1 with Rinkhals firmware via REST API polling
- **QR Scanner for spool assignment** - Camera-based or manual entry to assign spools to printer slots
- **Edit spool weight** - Pencil button to directly set remaining_weight_g (fixes A1/P1S showing 0g)
- **Color name lookup** - Scanner displays color names from hex codes (e.g., "PETG HF (Gray)")
- **Spool search improvements** - Global search finds spools by brand, material, QR code
- **Dedicated scanner support** - Enter key submits for USB/Bluetooth barcode scanners

### Changed
- Spool cards show printer nickname instead of "Printer X"
- Spool card buttons now icon-only with even spacing
- Spool search results show QR code for differentiation
- QR scan assignment sets spool_confirmed=true (no false warnings)

### Fixed
- Slot numbering 1-based consistency between UI and backend
- API path doubling in spool lookup endpoint
