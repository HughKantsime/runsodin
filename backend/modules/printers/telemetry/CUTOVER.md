# V2 Cutover — Remaining Operator Steps

The `telemetry-v2-cutover_20260417` track shipped all 10 BambuPrinter
callsite migrations + a full `BambuCommandAdapter` + `BambuV2StatusView`
+ session helpers. V2 is ready. The remaining work requires operator
decisions, not more autonomous code commits.

## Current state

- **Flag default:** `ODIN_TELEMETRY_V2=0` → legacy path runs.
- **V2 path:** reachable at `ODIN_TELEMETRY_V2=1`. Every route that used
  `BambuPrinter` has a flag-gated branch (10 callsites across 8 files).
- **Tests:** 348 telemetry contract tests pass, including 4 flag-routing
  tests that verify the branches fire correctly.
- **Legacy files** (`adapters/bambu.py`, `monitors/mqtt_printer.py`,
  `monitors/mqtt_telemetry.py`) still exist as fallback — cannot be
  deleted while legacy branches reference them.

## Step-by-step cutover (operator)

### Step 1 — Flip default to V2 in staging

Change `ODIN_TELEMETRY_V2` default from `"0"` to `"1"` in
`backend/modules/printers/telemetry/feature_flag.py` (line with
`os.environ.get(_ENV_VAR, "0")`), or set the env var explicitly in
staging's docker-compose / deployment config.

Deploy to staging. Validate against a real Bambu printer:
- `GET /printers/{id}/live-status` returns sensible values.
- Pause/resume/stop commands actually pause/resume/stop the printer.
- AMS sync returns the current AMS slots.
- Dispatch (FTPS upload + start_print) completes a small test print.
- The monitor daemon (`mqtt_printer.py` → V2 path) updates DB telemetry
  + fires alerts + handles state transitions.

If any of these diverge from legacy behavior, file a bug + flip back
to `ODIN_TELEMETRY_V2=0`.

### Step 2 — Soak in staging for one release cycle

At least 7 days of real traffic. Monitor `active_errors` in status
output — V2 surfaces HMS codes legacy dropped, so some alerts may look
new. That's intentional; don't panic.

### Step 3 — Flip default in production

Ship the same `feature_flag.py` change to prod. Observe for a few days.

### Step 4 — Delete legacy

Once prod has been stable on V2 for long enough that rollback isn't
a realistic concern:

1. **Strip legacy branches** from the 8 migrated route files. Each
   contains an `if is_v2_enabled(): ... else: <legacy path>` pattern;
   remove the else branch and the flag check.
2. **Delete `adapters/bambu.py`** — should be unreferenced by
   production code at this point. `grep -r "adapters.bambu" backend/`
   must return nothing (test files may still reference if they
   regress-test legacy behavior; rewrite those against V2).
3. **Delete `monitors/mqtt_printer.py`** and write a replacement that
   uses V2 directly (without the round-trip-to-dict shim that the
   current version uses for compatibility).
4. **Delete `monitors/mqtt_telemetry.py`** — Bambu-only per its
   docstring; unused after mqtt_printer.py is rewritten.
5. **Extract color utilities** from `bambu_integration.py` into a new
   `backend/modules/printers/color_utils.py`; delete the rest of
   `bambu_integration.py`.
6. **Remove the `feature_flag.py` module** — V2 is the only path now,
   no flag to honor.
7. **Retire this CUTOVER.md and `MIGRATION.md`**.

### Step 5 — Close both tracks

Mark `telemetry-rewrite-bambu-first` and `telemetry-v2-cutover` as
shipped. Tag ODIN v1.11.0.

## Rollback (if V2 breaks in prod after default flip)

Set `ODIN_TELEMETRY_V2=0` in production env — legacy path resumes
immediately. Code rollback not required (both paths coexist).

If that doesn't fix it (i.e. V2 corrupts state in a way legacy can't
recover from), revert all 10 migration commits. Legacy-only behavior
resumes. This is unlikely because V2 is read-mostly; the command
adapter is byte-equivalent to legacy and can't corrupt state legacy
wouldn't also corrupt.

## What V2 users see that legacy users didn't

Documented so operators aren't surprised:

- `FAILED` and `FINISHED` are now distinct from `IDLE` in canonical
  state. Legacy's collapse to IDLE is gone. Status routes that return
  the `state` field will show these new values when a print ends.
- `ERROR` state is overlaid on top of gcode_state when `print_error != 0`
  OR an HMS code has severity=error. Legacy never surfaced HMS-driven
  errors; V2 does. Alerts downstream of `_on_status` may fire more
  often for real printer issues that used to silently tick.
- `active_errors` list (V2-only field) exposes every HMS code the
  printer is broadcasting. The UI should render these as a list of
  badges; status endpoints expose them via `BambuV2StatusView.active_errors`.
- `stage_code` (V2-only) shows the specific Bambu stage number
  (0, 1, 2, 3, 4, 13, 14, 29, 39, 255, -1). Legacy rendered some of
  these as "Stage N" fallback strings.
