# Printer Telemetry V2

**Start here if you're touching any Bambu-printer code path.**

The legacy adapter (`../adapters/bambu.py`) is in the process of being
replaced by the V2 pipeline in this package. Both paths run today.
Which one handles a given request is decided by the
`ODIN_TELEMETRY_V2` environment variable.

## Current state (as of 2026-04-17)

| Env value | Behavior |
|---|---|
| `0` (default) | Legacy `BambuPrinter` handles everything |
| `1` | V2 adapters handle everything |
| `shadow` | Flag is reserved for side-by-side diff observation (not currently wired) |

## Quick decision tree for new code

**Are you writing new code that talks to a Bambu printer?**
→ Use V2. Period. Do not import `BambuPrinter`.

**Reading status?**
- One-shot (REST route): `session.read_status_once(config)` → returns a
  `BambuV2StatusView` with legacy-shaped attributes.
- Long-running monitor: instantiate `BambuTelemetryAdapter(config, emitter)`.

**Sending a command (gcode, pause/resume/stop, AMS control, lights)?**
- One-shot: `session.run_command(config, "method_name", *args)`.
- Long-running: instantiate `BambuCommandAdapter(config)`.

**Uploading a file (`.3mf`)?**
- `ftp_upload.upload_file(host, access_code, local_path)` — FTPS port 990,
  byte-equivalent to legacy's `BambuPrinter.upload_file`.

## What's here

```
telemetry/
├── base.py                  # _StrictBase (Pydantic model base)
├── events.py                # TelemetryEvent sealed union
├── feature_flag.py          # ODIN_TELEMETRY_V2 dispatch
├── observability.py         # UnmappedFieldObserver
├── parity.py                # V2-vs-legacy classifier (fixture-level)
├── live_replay.py           # LocalBroker (mosquitto subprocess) + publish_fixture
├── live_shadow.py           # V2-vs-legacy on live-MQTT fixtures
├── demo.py                  # DemoEngine (multi-printer replayer for marketing)
├── demo_cli.py              # CLI: `python -m ...demo_cli <scenario>`
├── replay.py                # In-process JSONL → events → transition()
├── state.py                 # Canonical PrinterState + PrinterStatus
├── transition.py            # Pure state-machine function
├── validators.py            # coerce_int_or_raise / coerce_float_or_raise
├── CUTOVER.md               # Operator step-by-step to finish legacy delete
├── MIGRATION.md             # Legacy callsite map (historical reference)
└── bambu/
    ├── adapter.py           # BambuTelemetryAdapter (ingestion)
    ├── commands.py          # BambuCommandAdapter (control plane)
    ├── enums.py             # BambuGcodeState
    ├── ftp_upload.py        # FTPS file upload helper
    ├── hms.py               # HMS event catalog + lookup
    ├── hms_codes.json       # Observed HMS codes (bootstrap data)
    ├── raw.py               # Pydantic models for raw MQTT payloads
    ├── session.py           # read_status_once + run_command
    └── status_view.py       # BambuV2StatusView + ams_slots_from_section
```

## The delete checklist

When operator validates V2 in prod, follow `CUTOVER.md` to:
1. Strip legacy branches from the 8 migrated route files.
2. Delete `../adapters/bambu.py`.
3. Delete `../monitors/mqtt_printer.py` (rewrite to use V2 directly
   without the model_dump round-trip shim).
4. Delete `../monitors/mqtt_telemetry.py` (Bambu-only per docstring).
5. Extract color utilities from `../bambu_integration.py` into
   `../color_utils.py`; delete the rest of `bambu_integration.py`.
6. Delete `feature_flag.py` — V2 is then the only path.
7. Delete `CUTOVER.md` and `MIGRATION.md`.

## Tests

`tests/test_telemetry/` has 348 contract tests covering every module
here. Run with `pytest tests/test_telemetry/`. The `telemetry-contracts`
job in `.github/workflows/ci.yml` gates every push + PR.
