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
#   1. Starts mosquitto on 127.0.0.1:1883.
#   2. Runs run_one_cell.sh with N=5 / D=30s (ODIN_TELEMETRY_V2=1 is baked
#      into the cell script).
#   3. Asserts `printer_telemetry` has rows > 0 in odin_stress.db.
#
# A row count of 0 = the V2 path silently dropped telemetry, which is
# exactly the failure mode the import-path canonicalization fixed.

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

probe_broker() {
    "$REPO/.venv-stress/bin/python3" -c "
import sys
import paho.mqtt.client as mqtt
c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
try:
    rc = c.connect('127.0.0.1', 1883, keepalive=3)
    c.disconnect()
    sys.exit(0 if rc == 0 else 1)
except Exception:
    sys.exit(1)
" 2>/dev/null
}

# Re-use an existing broker on :1883 if one's already serving MQTT (e.g.
# Colima or the docker-compose demo stack). Otherwise spin up our own.
if probe_broker; then
    echo "Re-using existing broker on 127.0.0.1:1883"
else
    cat >"$MOSQ_DIR/mosquitto.conf" <<'EOF'
listener 1883 127.0.0.1
allow_anonymous true
persistence false
log_dest stderr
log_type error
EOF

    mosquitto -c "$MOSQ_DIR/mosquitto.conf" >"$MOSQ_DIR/mosquitto.log" 2>&1 &
    MOSQ_PID=$!

    READY=0
    for _ in $(seq 1 25); do
        if probe_broker; then
            READY=1
            break
        fi
        sleep 0.2
    done
    if [ "$READY" -ne 1 ]; then
        echo "FAIL: mosquitto did not accept MQTT connections on 127.0.0.1:1883" >&2
        cat "$MOSQ_DIR/mosquitto.log" >&2 || true
        exit 1
    fi
    echo "Started ephemeral mosquitto (pid=$MOSQ_PID)"
fi

# Run the V2=1 cell (the script bakes ODIN_TELEMETRY_V2=1 in directly)
tests/stress/mqtt/run_one_cell.sh "$N" "$DURATION" "$CELL_DIR"

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

echo "OK: $ROWS rows ingested under ODIN_TELEMETRY_V2=1"
