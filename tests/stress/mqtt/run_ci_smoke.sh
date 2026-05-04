#!/usr/bin/env bash
# CI smoke for ODIN_TELEMETRY_V2=1.
#
# Closes the gap Codex flagged in the V2-flip pre-flip review: no CI test
# runs the full app/monitor with ODIN_TELEMETRY_V2=1 against a live broker.
# The `tests/test_telemetry/` suite runs the V2 *adapter* end-to-end, but
# `test_telemetry_flag_routing.py` monkeypatches `is_v2_enabled` — neither
# exercises the actual import path that produced the silent-drop bug.
#
# This wrapper:
#   1. Picks a free TCP port on 127.0.0.1.
#   2. Spawns an ephemeral mosquitto bound to that port.
#   3. Runs run_one_cell.sh with N=5 / D=30s + MOSQ_PORT=<random>.
#   4. Asserts `printer_telemetry` has rows > 0 in odin_stress.db.
#
# Why isolate (vs. re-using a pre-existing broker on :1883): retained
# messages, lingering subscriptions, or cross-run state on a shared broker
# can produce rows the publisher never sent this run, masking a real
# regression of the V2 path.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO"

N=5
DURATION=30
CELL_DIR="stress-out/ci-smoke-$(date -u +%Y%m%dT%H%M%SZ)"
MOSQ_DIR="$(mktemp -d -t odin-ci-smoke.XXXXXX)"

cleanup() {
    if [ -n "${MOSQ_PID:-}" ]; then
        kill "$MOSQ_PID" 2>/dev/null || true
    fi
    rm -rf "$MOSQ_DIR"
}
trap cleanup EXIT

# Preflight
if ! command -v mosquitto >/dev/null 2>&1; then
    echo "FAIL: mosquitto not on PATH" >&2
    exit 2
fi
if [ ! -x "$REPO/.venv-stress/bin/python3" ]; then
    echo "FAIL: .venv-stress missing on this runner — provision it first" >&2
    exit 2
fi

# Pick a free 127.0.0.1 TCP port. The kernel picks one when we bind to
# port 0; we read it back, close, then reuse.
MOSQ_PORT="$("$REPO/.venv-stress/bin/python3" -c "
import socket
s = socket.socket()
s.bind(('127.0.0.1', 0))
print(s.getsockname()[1])
s.close()
")"
echo "Using ephemeral mosquitto port: $MOSQ_PORT"

cat >"$MOSQ_DIR/mosquitto.conf" <<EOF
listener $MOSQ_PORT 127.0.0.1
allow_anonymous true
persistence false
log_dest stderr
log_type error
EOF

mosquitto -c "$MOSQ_DIR/mosquitto.conf" >"$MOSQ_DIR/mosquitto.log" 2>&1 &
MOSQ_PID=$!

# Wait for broker — paho connect at the picked port
READY=0
for _ in $(seq 1 25); do
    if "$REPO/.venv-stress/bin/python3" -c "
import sys
import paho.mqtt.client as mqtt
c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
try:
    rc = c.connect('127.0.0.1', $MOSQ_PORT, keepalive=3)
    c.disconnect()
    sys.exit(0 if rc == 0 else 1)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
        READY=1
        break
    fi
    sleep 0.2
done
if [ "$READY" -ne 1 ]; then
    echo "FAIL: mosquitto did not accept MQTT connections on 127.0.0.1:$MOSQ_PORT" >&2
    cat "$MOSQ_DIR/mosquitto.log" >&2 || true
    exit 1
fi
echo "Started ephemeral mosquitto (pid=$MOSQ_PID, port=$MOSQ_PORT)"

# Run the V2=1 cell on the ephemeral port. run_one_cell.sh threads
# MOSQ_PORT through to multi_publisher --broker-port and to
# ODIN_BAMBU_INSECURE_BROKER_PORT for the monitor's broker_policy.
MOSQ_PORT="$MOSQ_PORT" tests/stress/mqtt/run_one_cell.sh "$N" "$DURATION" "$CELL_DIR"

# Assert telemetry ingested under V2
ROWS=$("$REPO/.venv-stress/bin/python3" -c "
import sqlite3
conn = sqlite3.connect('$REPO/odin_stress.db')
print(conn.execute('SELECT COUNT(*) FROM printer_telemetry').fetchone()[0])
")
echo "printer_telemetry rows: $ROWS"

if [ "$ROWS" -eq 0 ]; then
    echo "FAIL: ODIN_TELEMETRY_V2=1 did not ingest any rows in ${DURATION}s with N=${N}" >&2
    echo "Cell artifacts: $CELL_DIR" >&2
    exit 1
fi

echo "OK: $ROWS rows ingested under ODIN_TELEMETRY_V2=1 (port=$MOSQ_PORT)"
