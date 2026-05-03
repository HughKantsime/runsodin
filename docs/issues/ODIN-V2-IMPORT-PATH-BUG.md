# ODIN-V2 latent import-path bug — silently drops all telemetry when V2 flag flips

**Severity**: latent / would be CRITICAL on `ODIN_TELEMETRY_V2=1` flag-flip
**Discovered**: 2026-05-03 during MQTT stress-test scaffolding
**Affects**: every code path that flips through the V2 emit shim
**Currently in prod?**: NO — `ODIN_TELEMETRY_V2` defaults to `0`, so `_connect_v2` is never reached. The bug is dormant. Flipping the flag without this fix produces a silent, fleet-wide telemetry blackout.

## Symptom

With `ODIN_TELEMETRY_V2=1`:

- `BambuTelemetryAdapter._on_message` runs (paho callbacks fire)
- `_process_event` runs (state machine executes)
- The emit closure receives `BambuReportEvent` instances (verified by interposing a logging emitter)
- BUT `_on_status` is never invoked
- BUT zero rows land in `printer_telemetry`, `ams_telemetry`, `alerts`
- BUT no error or warning is logged anywhere

The dashboard shows every printer as Offline. The only signal that anything is wrong is the absence of telemetry rows.

## Root cause

The V2 emit shim in `backend/modules/printers/monitors/mqtt_printer.py:121-125`:

```python
def emit(item):
    if isinstance(item, BambuReportEvent):
        section_dict = item.section.model_dump(exclude_none=False)
        on_status(_LegacyStatusShim(section_dict))
```

The `BambuReportEvent` referenced here is imported from `modules.printers.telemetry.events` (line 112).

The adapter that *creates* `BambuReportEvent` instances at `backend/modules/printers/telemetry/bambu/adapter.py:38-44` imports from `backend.modules.printers.telemetry.events`:

```python
from backend.modules.printers.telemetry.events import (
    BambuInfoEvent,
    BambuReportEvent,
    ConnectionEvent,
    DegradedEvent,
    TelemetryEvent,
)
```

When `PYTHONPATH` includes both the repo root AND `backend/` (typical of any setup that runs both the FastAPI app via `python -m uvicorn main:app` from `backend/` and any tooling from the repo root), Python loads the same source file twice as two distinct modules:

- `modules.printers.telemetry.events` — one module object, one `BambuReportEvent` class
- `backend.modules.printers.telemetry.events` — a second module object, a second `BambuReportEvent` class

The two classes are byte-identical but are different Python objects. `isinstance(item, BambuReportEvent)` returns `False`. The shim drops the event silently. `_on_status` never runs. No DB write. No log line.

## How prod survives

Prod runs `python -m modules.printers.monitors.mqtt_monitor` from `cwd=/app/backend`, so `PYTHONPATH` is effectively only `/app/backend`. With only one root, `from backend.modules...` would `ImportError` outright — but adapter.py is only imported when `ODIN_TELEMETRY_V2=1`, and the flag defaults off, so the import never happens. The `from backend.modules...` lines are unreachable code in prod today.

## Scope

`grep -rE "^from backend\." backend/modules/printers/telemetry/` returns **42 import sites** across the V2 telemetry path:

```
backend/modules/printers/telemetry/parity.py
backend/modules/printers/telemetry/transition.py
backend/modules/printers/telemetry/events.py
backend/modules/printers/telemetry/demo.py
backend/modules/printers/telemetry/demo_cli.py
backend/modules/printers/telemetry/replay.py
backend/modules/printers/telemetry/live_replay.py
backend/modules/printers/telemetry/live_shadow.py
backend/modules/printers/telemetry/bambu/hms.py
backend/modules/printers/telemetry/bambu/raw.py
backend/modules/printers/telemetry/bambu/adapter.py
... (~10 more files)
```

Every one of these violates ODIN's import convention (everywhere else in `backend/modules/` uses `from modules.*`).

## Fix

Two options:

**Option A (recommended)**: Find/replace `from backend.modules.printers.telemetry` → `from modules.printers.telemetry` across the 42 sites. Matches the rest of the codebase. One-line bash:

```sh
grep -rlE "^from backend\.modules\.printers\.telemetry" backend/modules/printers/telemetry/ \
    | xargs sed -i '' 's|^from backend\.modules\.printers\.telemetry|from modules.printers.telemetry|'
```

Run the V2 telemetry test suite after to verify nothing regresses (the imports already work because Python loads the module under either name; switching to one name just unifies them).

**Option B (transient workaround)**: At process startup, alias both names in `sys.modules` so they resolve to the same module object. This is what `tests/stress/mqtt/run_monitor.py` does as a stress-test-only workaround:

```python
import importlib, sys
for suffix in (
    "modules.printers.telemetry.events",
    "modules.printers.telemetry.bambu.adapter",
    ...
):
    mod = importlib.import_module(suffix)
    sys.modules[f"backend.{suffix}"] = mod
```

Don't ship Option B to prod — Option A is the real fix.

## Verification

After fixing, with `ODIN_TELEMETRY_V2=1` and a synthetic Bambu publisher, confirm:

- `_on_status` latency entries appear in any instrumentation that wraps it
- Rows land in `printer_telemetry` table at the expected ~6/min/printer rate
- `BambuTelemetryAdapter._on_message` and `PrinterMonitor._on_status` both produce roughly equal call counts (one shim hop apart)

## How this was found

While building the MQTT scalability stress-test scaffold (`tests/stress/mqtt/`), the publisher was firing valid messages, the V2 adapter was parsing them, but `printer_telemetry` row count stayed at zero. Tracing through `_on_message → _process_event → _emitter`, an interposed logging emitter showed `BambuReportEvent` instances flowing into `emit()`. Adding `id()` checks to the two import paths confirmed they resolve to two distinct class objects.

## Why no test caught this

V2 has a unit test suite (`tests/test_telemetry/...`) but it instantiates the adapter and asserts on emitted events directly — never through the `_LegacyStatusShim` bridge in `mqtt_printer._connect_v2`. The shim's `isinstance` check is the only place where two import paths have to agree, and it's not exercised by any V2 test. Add an integration test that boots the monitor process and asserts a single fixture replay produces a `printer_telemetry` row.
