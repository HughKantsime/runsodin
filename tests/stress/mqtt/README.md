# MQTT stress-test scaffold

Measure ODIN's behavior at N concurrent Bambu MQTT printers. Drives one
captured fixture into N synthetic streams against a real backend, instruments
the message hot path, sweeps `(printer count) × (db backend) × (telemetry V2 flag)`.

This is a **measurement** scaffold, not optimization. Results inform whether
the bambuddy thread's "MQTT hosed at 20 printers" reproduces here, and where
the knee is on our setup.

## Bottleneck hypothesis (going in)

Code-cited from a 2026-05-02 read of the message path:

1. **SQLite single-writer.** `_on_status` (`backend/modules/printers/monitors/mqtt_printer.py:301`)
   opens a fresh `sqlite3.connect()` per printer per heartbeat (10s throttle),
   does UPDATE printers + INSERT printer_telemetry + **DELETE FROM
   printer_telemetry WHERE recorded_at < now-90d on every insert** (line 393,
   full-table scan amortized on the hot path). WAL gives concurrent reads but
   only one writer; `busy_timeout=5000ms`. At 50 printers, burst-aligned
   heartbeats block on the timeout, then the `try/except` swallows them.
2. **paho `_on_message` runs synchronously in the network loop thread.** Any
   DB write latency stalls that printer's TCP recv. Broker starts QoS1
   retransmits, message order can shift on reconnect.
3. **Per-printer paho.mqtt.Client + per-printer thread + per-printer TLS
   context.** N=50 means 50 TLS handshakes on cold start and on every
   reconnect, all serial in `_check_reconnect` (`mqtt_monitor.py:222`).
4. **Postgres path exists** (`backend/core/db.py:51-60` QueuePool 10+20)
   **but prod is SQLite** (LXC 112). The matrix lets us prove or disprove
   that Postgres shifts the ceiling.

## First sweep results (2026-05-03)

5-minute cells against sqlite + V2 telemetry, 10s heartbeat rate, captured
fixture replayed against a real backend + monitor.

| N printers | _on_status p50 | p95 | p99 | max | publishes | DB writes |
|---:|---:|---:|---:|---:|---:|---:|
| 10 | 8.0ms | 12.1ms | 16.0ms | 133ms | 299 | +44 |
| 25 | 8.2ms | 12.4ms | 17.2ms | 111ms | 751 | +106 |
| 50 | 8.6ms | 12.9ms | 17.4ms | 110ms | 1502 | +209 |

**Hypothesis falsified for this regime.** Latency is nearly flat from 10→50
printers. The "20-printer hose-up" pattern from the bambuddy thread did NOT
reproduce. WAL + 5s busy_timeout + the 10s heartbeat throttle handle 50
printers without contention. Zero publisher failures, zero dropped messages.

**Caveats** — the sweep doesn't prove ODIN scales infinitely. It proves:
- The bottleneck I hypothesized from the code read is NOT the ceiling at this
  size + this duration.
- The DELETE-on-every-insert is cheap when `printer_telemetry` has only ~250
  rows; the same write at month-scale (millions of rows) is unproven.
- Cold-connection storm (50 simultaneous TLS handshakes) is unproven — the
  V2 bypass uses plaintext, so we never hit real TLS cost.
- 100/200 printers untested — the actual ceiling could be much higher.
- Postgres untested — the matrix never executed the postgres half because
  the sqlite half didn't blink.

**Latent prod bug found in the process** — see
[`docs/issues/ODIN-V2-IMPORT-PATH-BUG.md`](../../../docs/issues/ODIN-V2-IMPORT-PATH-BUG.md).
The V2 telemetry adapter's `_LegacyStatusShim` silently drops every event when
both `backend.modules.*` and `modules.*` are on the path because the two
import roots produce two distinct `BambuReportEvent` classes. Currently
unreachable in prod (V2 flag-gated off) but flipping the flag without the fix
would produce a fleet-wide telemetry blackout.

## Components

| File | Purpose |
|---|---|
| `fixture_multiplier.py` | Multiply one `.jsonl` fixture into N synthetic-serial streams |
| `multi_publisher.py` | Spawn N paho clients firing those streams against the broker |
| `bootstrap_printers.py` | POST N synthetic printers to backend so the subscriber binds |
| `instrument.py` | Opt-in monkeypatch of `_on_status` / `_on_message` for latency, plus row sampler |
| `report.py` | Read one cell's outputs → markdown summary |
| `run_sweep.sh` | Orchestrate the full matrix |

## Quick run (one cell, manual)

Terminal 1: bring up the broker.

```sh
docker compose -f ops/demo/docker-compose.demo.yml up -d mosquitto
```

Terminal 2: start backend with instrumentation. The instrumentation install
needs to be triggered from `backend/core/app.py` startup — there's a small
hook block to add (see "Wiring the instrumentation" below). Until that lands
you can install it from a shell first:

```sh
export ODIN_STRESS_INSTRUMENTATION="$(pwd)/stress-out/manual-$(date +%s)"
export ODIN_DB_PATH=odin_stress.db
export DATABASE_URL="sqlite:///./odin_stress.db"
export ODIN_TELEMETRY_V2=0
export API_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
export ODIN_DEMO_API_KEY="$API_KEY"
export ODIN_ALLOW_INSECURE_BAMBU_BROKER=1
export ODIN_BAMBU_INSECURE_BROKER_HOSTS=mosquitto,127.0.0.1,localhost
rm -f odin_stress.db*
python -m uvicorn backend.core.app:app --host 127.0.0.1 --port 8000
```

Terminal 3: seed printers + run publisher + render report.

```sh
RUN_DIR="$ODIN_STRESS_INSTRUMENTATION"
python -m tests.stress.mqtt.bootstrap_printers --count 10 --out $RUN_DIR/bootstrap.json
python -m tests.stress.mqtt.multi_publisher --printers 10 --duration 60 --out $RUN_DIR/publisher.json
python -m tests.stress.mqtt.report --run-dir $RUN_DIR
cat $RUN_DIR/REPORT.md
```

## Full sweep

```sh
# sqlite-only sweep at 10/25/50 printers, 5-minute cells
tests/stress/mqtt/run_sweep.sh --counts "10 25 50" --duration 300

# include postgres
tests/stress/mqtt/run_sweep.sh \
    --counts "10 25 50 100" \
    --pg-url "postgresql://odin:odin@localhost:5432/odin_stress" \
    --duration 600
```

Output lives at `stress-out/sweep-<UTC-ts>/`:
- `n10-sqlite-v20/REPORT.md` — one per cell
- `SWEEP.md` — concatenated cell reports

## Wiring the instrumentation

The cleanest hook is a 4-line gate near the top of
`backend/core/app.py` (or wherever the FastAPI app is constructed), inside
the application factory **after** the printer modules are importable but
**before** the MQTT monitor is started:

```python
import os
if os.environ.get("ODIN_STRESS_INSTRUMENTATION"):
    from tests.stress.mqtt.instrument import install
    install(os.environ["ODIN_STRESS_INSTRUMENTATION"])
```

This is gated on env var — when unset, production code paths are
byte-identical. We deliberately don't bake the import in unconditionally so
`tests/` stays a test-only dependency.

## Reading the report

Per cell you get:
- `_on_status` / `_on_message` p50/p95/p99/max latency
- Total publishes / failures from publisher side
- DB row growth (`printer_telemetry`, `ams_telemetry`, `alerts`) start → end

What "the knee" looks like:
- p99 latency walking up exponentially as N grows
- `printer_telemetry` row growth flatlines despite publisher still firing
  (writes are being dropped on busy_timeout)
- `failures` non-zero on publisher side (broker started rejecting)

What "no knee" looks like at our test sizes:
- p99 < 50ms even at N=100
- Publisher failures = 0
- Row growth ≈ N × duration / rate

## Hard stops / known constraints

- **Synthetic serial collision**: We use prefix `00M00A45000` + 4-digit
  index. Real Bambu serials in fixtures use different prefixes — no collision
  expected, but if a future printer-create endpoint adds serial validation
  this scaffold is what'll catch it.
- **`broker_policy` allowlist**: backend must boot with
  `ODIN_BAMBU_INSECURE_BROKER_HOSTS=mosquitto,127.0.0.1,localhost` and
  `ODIN_ALLOW_INSECURE_BAMBU_BROKER=1`. The sweep script sets this; manual
  runs need it too.
- **Postgres bootstrap**: this scaffold doesn't migrate a fresh postgres for
  you. Bring up your own DB and pass `--pg-url`. Migrations need to have run
  before `run_sweep.sh` starts a cell.
- **No mosquitto auth**: assumes `allow_anonymous true` per the demo
  `mosquitto.conf`. Real broker auth would need credentials threaded through
  `multi_publisher.py`.
- **100-printer cell may hang the backend**. That **is** a finding — kill it,
  mark the cell, move on.

## Follow-up — file ODIN-146

After the first sweep produces baseline numbers, file ODIN-146 with the
proposed optimizations:
1. Pull `DELETE FROM printer_telemetry WHERE recorded_at < ...` out of
   `_on_status` into a 1/hour background sweep.
2. Single writer thread + bounded `queue.Queue` between paho `_on_message`
   and the DB writer. Decouples message ingest from write latency.
3. Default new ODIN instances above ~25 printers to Postgres (config
   guidance, not code change).

Re-run the sweep after each optimization and compare `SWEEP.md` files.
