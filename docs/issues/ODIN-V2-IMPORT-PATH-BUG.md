# V2 telemetry import-path canonicalization

**Status**: ✅ FIXED in commits below (2026-05-04)
**Production impact at any point**: zero — V2 telemetry is feature-flag-gated
behind `ODIN_TELEMETRY_V2`, defaulted off, and the affected code was never
reached at runtime in any deployed environment.

## What changed

Canonicalized 42 import sites across `backend/modules/printers/telemetry/` and
43 import sites across `tests/test_telemetry/` from
`from backend.modules.printers.telemetry...` to `from modules.printers.telemetry...`,
matching the rest of the codebase's single-root convention
(`PYTHONPATH=/app/backend` everywhere — module entry points, supervisord,
test conftest).

## Why this matters

Python treats `backend.modules.foo` and `modules.foo` as two distinct module
objects when both roots are reachable on `sys.path`, even when they resolve
to the same source file. Two distinct module objects → two distinct class
definitions → `isinstance(obj, ClassFromRootA)` returns False for instances
created by `ClassFromRootB`. ODIN's V2 telemetry path used a `_LegacyStatusShim`
in `backend/modules/printers/monitors/mqtt_printer.py` that imported
`BambuReportEvent` via `modules.*` and ran `isinstance` against events
emitted by `BambuTelemetryAdapter`, which imported the class via
`backend.modules.*`. Two roots on `sys.path` → check returns False → the
shim silently dropped every event.

## How it was caught

Discovered while building the MQTT stress-test scaffold
(`tests/stress/mqtt/`). The scaffold injects `sys.path` entries that
matched the dual-root condition, so the latency/throughput numbers were
zero with V2 enabled — same code path that would activate the moment
`ODIN_TELEMETRY_V2=1` flipped on a real deployment. Caught and patched
before V2 ever rolled to a customer.

## Verification

- All 371 V2 telemetry tests under `tests/test_telemetry/` pass on the
  canonicalized imports.
- Smoke run of `tests/stress/mqtt/run_one_cell.sh` with V2 enabled
  produces `_on_status` and `_on_message` latency entries through the
  natural import path (no workaround), confirming the shim now
  forwards events correctly.

## Commits

- 18054a2  fix(telemetry-v2): canonicalize 85 import sites to single sys.path root

## Why this doc still exists

The discovery and fix are both part of ODIN's quality story. Instead of
deleting the record, this entry stays as evidence of the import-hygiene
sweep — one more case of "found by tooling we built, fixed before
anyone hit it."
