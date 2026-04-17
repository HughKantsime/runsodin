# Legacy Bambu Adapter — Migration Notes

**Context:** Track `telemetry-rewrite-bambu-first_20260417` shipped the V2 telemetry
pipeline. V2 is **ingestion-only** by design — it reads MQTT reports and derives
canonical `PrinterStatus`. The legacy `backend/modules/printers/adapters/bambu.py`
class (`BambuPrinter`) carries both ingestion AND control-plane methods
(`send_gcode`, filament controls, etc.).

Phase 8 of the track (delete legacy) is **blocked** on migrating 10 call sites
away from `BambuPrinter` for their control-plane needs. This file is the map.

## Call sites

From `conductor/tracks/telemetry-rewrite-bambu-first_20260417/plan.md` (Phase 0
T0.1 caller map, audited 2026-04-17):

| File | Line | What it does with `BambuPrinter` |
|---|---:|---|
| `backend/modules/vision/detection_thread.py` | 339 | pause-on-detection (vision alerts → pause printer) |
| `backend/modules/system/routes_setup.py` | 202 | initial printer setup / `test_connection` |
| `backend/modules/printers/monitors/mqtt_printer.py` | 23 | long-lived telemetry monitor — **replace entirely with V2 adapter** |
| `backend/modules/printers/routes_ams.py` | 177 | AMS slot management |
| `backend/modules/printers/routes_controls.py` | 344 | send gcode, pause/resume/stop |
| `backend/modules/printers/dispatch.py` | 198 | job dispatch (pick adapter) |
| `backend/modules/printers/route_utils.py` | 323, 396 | status reads + control helpers |
| `backend/modules/printers/routes_status.py` | 41 | status endpoints — **replace with V2 `PrinterStatus`** |
| `backend/modules/printers/routes_crud.py` | 428 | CRUD for printer records |

## Control-plane methods legacy provides (that V2 doesn't)

- `connect()` / `disconnect()` — V2 has equivalent `start()` / `stop()`.
- `send_gcode(cmd: str) -> bool` — not in V2.
- `pause_print()` / `resume_print()` / `stop_print()` — not in V2.
- `send_led_ctrl(...)` — not in V2.
- `get_ftp_files()` / file uploads — not in V2.
- `test_connection()` (static helper) — `test_bambu_connection` in `bambu_integration.py`; keep.
- AMS filament set / unload — not in V2.

## Recommended migration path

Split into two sub-tracks:

### Sub-track A — Status reads migrate to V2 (safe, minimal)

The hot spots: `routes_status.py`, `route_utils.py`, `routes_crud.py`. They
call `printer.status.X` on `BambuPrinter`. Migration:

1. Where V2 adapter is running (controlled by `ODIN_TELEMETRY_V2` feature flag),
   route callers to `v2_registry.get(printer_id).status()` instead of
   `BambuPrinter.status`.
2. Map V2's canonical `PrinterStatus` fields to the fields legacy exposed:
   - `state.value` → legacy's `state` enum (translate via
     `telemetry.parity._KNOWN_STATE_DIFFS` inverse lookup).
   - `progress_percent` → `print_progress`.
   - `bed_temp` → `bed_temp` (direct).
   - `active_errors` → new field on status DTO (legacy didn't expose).
3. Routes that need HMS, stage_code, job_id, firmware_versions — newly
   available under V2; add to DTO at the same time.

Blast radius: status DTO shape change. Add contract tests asserting DTO keys
are unchanged for legacy compatibility.

### Sub-track B — Control plane moves into a dedicated `BambuCommandAdapter`

The operations that mutate the printer (`send_gcode` etc.) are fundamentally
different from telemetry ingestion. Extract them into a new class:

    backend/modules/printers/telemetry/bambu/commands.py
    ├── BambuCommandAdapter
    │   ├── __init__(config: BambuAdapterConfig)
    │   ├── send_gcode(cmd) -> bool
    │   ├── pause_print() -> bool
    │   ├── resume_print() -> bool
    │   ├── stop_print() -> bool
    │   └── set_led(...) -> bool
    └── Uses the same paho MQTT client pattern as the V2 telemetry adapter,
        but publishes to `device/<serial>/request` instead of subscribing
        to `.../report`.

Routes that issued commands via `BambuPrinter.send_gcode()` call
`BambuCommandAdapter.send_gcode()` instead. Both adapters can coexist with
the same printer — different topics, no conflict.

### Sub-track C — Delete legacy

After A + B ship and a full release cycle passes with V2 in the primary
ingestion path:

1. Grep confirms zero imports of `adapters.bambu` remain in production code.
2. Delete `backend/modules/printers/adapters/bambu.py`.
3. Delete the Bambu-specific helpers in `bambu_integration.py` (keep color
   utilities — extract to `color_utils.py` first).
4. Delete `backend/modules/printers/monitors/mqtt_printer.py` and
   `backend/modules/printers/monitors/mqtt_telemetry.py` (Bambu-only per
   docstring).
5. PrusaLink + Elegoo stay on their own adapters — untouched per SPEC §9
   decision.

## Why this couldn't ship in the current track

The current track was scoped to **ingestion** — the "read printer state
correctly" half. Extending into control plane would have:
- Doubled the scope (same amount of code, new testing surface).
- Required real Bambu printer access to validate command paths (press "pause"
  on the real device, verify it responds).
- Blocked on the same constraint that blocked T7.4 (no live hardware for
  autonomous validation).

The V2 ingestion pipeline is shippable stand-alone behind the
`ODIN_TELEMETRY_V2` flag. Control-plane migration is a separate, later
effort with its own scope + risk profile.

## Safe state while this is blocked

- Legacy `BambuPrinter` continues to handle both ingestion and control in
  production (default `ODIN_TELEMETRY_V2=0`).
- V2 pipeline is fully shippable, fully tested, wired into the feature flag.
- Parity + live-shadow tests prove V2 can replace legacy's ingestion half
  with zero unclassified regressions.
- When an operator is ready, `ODIN_TELEMETRY_V2=1` moves ingestion to V2
  while legacy continues handling control. No code change, no delete.
