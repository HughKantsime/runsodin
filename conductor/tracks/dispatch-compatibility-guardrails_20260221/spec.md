# Spec: Dispatch Compatibility Guardrails

**Track ID**: dispatch-compatibility-guardrails_20260221
**Type**: feature
**Created**: 2026-02-21

---

## Goal

Make `AUTO_DISPATCH` safe to run in production by adding bed-size and printer-type compatibility checks between uploaded print files and the target printer. Currently the only guard is a file-extension check (`.3mf` → Bambu only). A `.gcode` sliced for a 350mm bed can be dispatched to a 220mm printer with no warning, and will fail mid-print.

Success = a file sliced for the wrong bed is blocked before it reaches the printer, with a clear message explaining the mismatch.

---

## Requirements

### R1 — Extract bed dimensions from uploaded files

At upload time, parse the print file for the bed size it was sliced for:

- **`.gcode` / `.bgcode`**: scan the first 100 lines for slicer comments:
  - PrusaSlicer: `; bed_size_x = 256.00`, `; bed_size_y = 256.00`
  - Cura: `; machine_width = 220`, `; machine_depth = 220` (also `;MINX`/`;MAXX` as fallback)
  - Bambu gcode: `; plate_x = ...` or `; print_size_x = ...`
- **`.3mf`**: zip file containing XML. Check:
  - `Metadata/slice_info.config` — Bambu-specific, has machine model
  - `Metadata/model_settings.config` — PrusaSlicer, has `bed_shape` entries
  - Fall back to inferring from `printer_model` if bed XML is absent

Store extracted values in `print_files.bed_x_mm` and `print_files.bed_y_mm` (nullable floats — if we can't extract, we skip the check rather than blocking).

### R2 — Store compatible API types on the file

At upload time, record which printer protocol types can print this file:

- `.3mf` → `compatible_api_types = "bambu"`
- `.gcode` or `.bgcode` → `compatible_api_types = "moonraker,prusalink,elegoo"`

Store as comma-separated string in new `print_files.compatible_api_types TEXT` column.

### R3 — Add bed dimensions to printer configuration

Printers need a configured bed size for the check to work:

- Add `bed_x_mm REAL` and `bed_y_mm REAL` to the `printers` table (nullable — existing printers default to NULL, meaning no check performed)
- Expose these fields in the printer add/edit form (UI)
- Pre-populate known values for common models via a lookup table in code (e.g. Bambu X1C → 256×256, Bambu A1 → 256×256, Prusa MK4 → 250×210, Creality Ender 3 → 220×220)

### R4 — Dispatch guard in `dispatch_job()`

Before uploading a file, `dispatch_job()` in `printer_dispatch.py` must check:

1. **API type compatibility**: if `print_files.compatible_api_types` is set and the printer's `api_type` is not in that list → block with clear error
2. **Bed size**: if both `print_files.bed_x_mm`/`bed_y_mm` and `printers.bed_x_mm`/`bed_y_mm` are set → check `file_bed ≤ printer_bed` (with 2mm tolerance for floating-point slop). If either dimension exceeds the printer bed → block with error like:
   > "File was sliced for a 350×350mm bed, but Ender-3 has a 220×220mm bed. Upload a re-sliced version."

Both checks are soft-fail if data is missing (NULL bed dimensions → check skipped, no `compatible_api_types` → check skipped).

### R5 — UI: show compatibility info on model/file detail

On the Models page, print file entries should display:
- Bed dimensions if extracted (e.g., "256×256mm")
- Compatible printer types badge ("Bambu only" or "Moonraker / PrusaLink")
- A warning icon if the file has never been successfully dispatched and dimensions are unknown (nudge to re-upload with a slicer that embeds the metadata)

### R6 — UI: incompatibility warning on job scheduling

In the job create/edit modal, if the selected printer's bed is smaller than the file's sliced bed, show an inline warning. Don't block — warn and let the operator override.

---

## Acceptance Criteria

- [ ] Uploading a `.gcode` sliced for 350×350mm and attempting to dispatch to a 220mm printer returns HTTP 400 with a clear message
- [ ] Uploading a `.3mf` and attempting to dispatch to a Moonraker printer returns HTTP 400 (this already works — verify it still does)
- [ ] Uploading a `.gcode` with no slicer comments (bed unknown) dispatches without error (soft-fail)
- [ ] Printers table accepts `bed_x_mm` / `bed_y_mm` and they appear in the printer add/edit form
- [ ] Models page shows bed dimensions for print files where they could be extracted
- [ ] All existing tests pass (839 baseline)

---

## Out of Scope

- Nozzle diameter compatibility check (nozzle_diameter already stored on print_files and printers, but matching logic is complex — leave for later)
- Material compatibility (PLA vs PETG vs ASA — no data model for "printer supports material X")
- Slicing integration (we never slice inside ODIN — file always comes pre-sliced)
- Blocking dispatch if bed info is missing — soft-fail only, never silently block unknown files
- bgcode binary parsing — treat bgcode as opaque, skip metadata extraction (extension-only check still applies)

---

## Technical Notes

### DB migration pattern
New columns are added in `entrypoint.sh` using `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` pattern (not Alembic — existing pattern in the codebase). Also add them to the SQLAlchemy `Printer` model.

### Metadata extraction module
Create `backend/print_file_meta.py` — standalone module, no DB imports. Accepts a file path + extension, returns a dict `{bed_x_mm, bed_y_mm, compatible_api_types}`. Called from `routers/models.py` after the file is written to disk.

### Known printer bed sizes lookup
In `print_file_meta.py`, define a `KNOWN_PRINTER_BEDS` dict keyed by lowercase model string fragments:
```python
KNOWN_PRINTER_BEDS = {
    "x1c": (256, 256), "x1 carbon": (256, 256),
    "p1s": (256, 256), "p1p": (256, 256),
    "a1 mini": (180, 180), "a1": (256, 256),
    "mk4": (250, 210), "mk3": (250, 210),
    "mini": (180, 180),
    "ender 3": (220, 220), "ender-3": (220, 220),
    "voron": (300, 300),
}
```

### Where dispatch guard lives
All checks go in `dispatch_job()` in `printer_dispatch.py`, after credential loading and before routing to the protocol handler. Single function, early returns with clear messages.

### Printer form
The printer add/edit modal (`frontend/src/pages/Printers.jsx`) currently has api_type, api_host, api_key fields. Add `bed_x_mm` and `bed_y_mm` as optional numeric inputs in a "Bed Size" row. Pre-fill from lookup when the user types a known model name.
