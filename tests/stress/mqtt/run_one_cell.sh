#!/usr/bin/env bash
# Run one stress cell: N printers, given duration. Assumes mosquitto is up
# on host:1883 (script doesn't manage it — re-use across cells avoids
# repeated container churn).
#
# Args: <N_printers> <duration_seconds> <cell_dir>
#
# Side effects: starts and stops backend (uvicorn) + monitor (run_monitor.py)
# and wipes odin_stress.db. Each cell is independent.

set -euo pipefail

if [ $# -lt 3 ]; then
    echo "usage: $0 <N> <duration_s> <cell_dir>" >&2
    exit 2
fi

N="$1"
DURATION="$2"
CELL_DIR="$3"
# MOSQ_PORT defaults to 1883 to preserve existing sweep behavior. The CI
# smoke (run_ci_smoke.sh) overrides this so each run gets an isolated
# broker — stale retained messages can otherwise mask a real V2 path
# regression by producing rows the publisher never sent this run.
MOSQ_PORT="${MOSQ_PORT:-1883}"
mkdir -p "$CELL_DIR"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO"

PYBIN="$REPO/.venv-stress/bin/python3"

# The device destination policy refuses loopback printers. The broker here is an
# ephemeral mosquitto on 127.0.0.1, so the backend and monitor load the TEST-ONLY
# exemption for exactly 127.0.0.1 and ::1 (tests/support/loopback_device_policy).
LOOPBACK_DEVICES="$REPO/tests/support/loopback_device_policy"

# All credentials below are for an EPHEMERAL local SQLite stress DB that
# only exists during this script's run. They grant zero access to anything
# outside this process. Generated at runtime so nothing pattern-matches a
# secret scanner if this file is ever committed.
ENC_KEY="$(cat /tmp/odin-stress-enc-key 2>/dev/null || "$PYBIN" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())' | tee /tmp/odin-stress-enc-key)"
JWT_KEY="$(cat /tmp/odin-stress-jwt-key 2>/dev/null || "$PYBIN" -c 'import secrets; print(secrets.token_urlsafe(48))' | tee /tmp/odin-stress-jwt-key)"
ADMIN_PW="$(cat /tmp/odin-stress-admin-pw 2>/dev/null || "$PYBIN" -c 'import secrets; print(secrets.token_urlsafe(24))' | tee /tmp/odin-stress-admin-pw)"
API_KEY_VAL="$(cat /tmp/odin-stress-api-key 2>/dev/null || "$PYBIN" -c 'import secrets; print(secrets.token_urlsafe(24))' | tee /tmp/odin-stress-api-key)"

cleanup() {
    if [ -f /tmp/odin-stress-monitor.pid ]; then
        kill "$(cat /tmp/odin-stress-monitor.pid)" 2>/dev/null || true
        rm -f /tmp/odin-stress-monitor.pid
    fi
    if [ -f /tmp/odin-stress-backend.pid ]; then
        kill "$(cat /tmp/odin-stress-backend.pid)" 2>/dev/null || true
        rm -f /tmp/odin-stress-backend.pid
    fi
}
trap cleanup EXIT

# Wipe DB
rm -f odin_stress.db odin_stress.db-wal odin_stress.db-shm

# Schema: the canonical bootstrap, the same command docker/entrypoint.sh runs
# (scripts.bootstrap_database), then the stress admin user.
{
(cd backend && ODIN_DB_ROLE=bootstrap DATABASE_URL="sqlite:///$REPO/odin_stress.db" "$PYBIN" -m scripts.bootstrap_database)
STRESS_ADMIN_PW="$ADMIN_PW" PYTHONPATH=backend "$PYBIN" -c "
import os, sqlite3
from passlib.context import CryptContext
hash_ = CryptContext(schemes=['bcrypt']).hash(os.environ['STRESS_ADMIN_PW'])
c = sqlite3.connect('odin_stress.db')
c.execute('INSERT OR IGNORE INTO users (username, password_hash, role, is_active, mfa_enabled) VALUES (?, ?, ?, ?, ?)',
          ('stress-admin', hash_, 'admin', 1, 0))
c.commit()
print('migrate done')
"
} >"$CELL_DIR/migrate.log" 2>&1

# Start backend
DATABASE_URL="sqlite:///$REPO/odin_stress.db" \
DATABASE_PATH="$REPO/odin_stress.db" \
ENCRYPTION_KEY="$ENC_KEY" \
JWT_SECRET_KEY="$JWT_KEY" \
API_KEY="$API_KEY_VAL" \
ODIN_TELEMETRY_V2=1 \
ODIN_ALLOW_INSECURE_BAMBU_BROKER=1 \
ODIN_BAMBU_INSECURE_BROKER_HOSTS='mosquitto,127.0.0.1,localhost' \
ODIN_BAMBU_INSECURE_BROKER_PORT="$MOSQ_PORT" \
ODIN_STRESS_INSTRUMENTATION="$CELL_DIR" \
ODIN_DB_PATH="$REPO/odin_stress.db" \
PYTHONPATH="$LOOPBACK_DEVICES:backend" \
"$PYBIN" -m uvicorn main:app --host 127.0.0.1 --port 8000 \
    >"$CELL_DIR/backend.log" 2>&1 &
echo $! > /tmp/odin-stress-backend.pid

# Wait for /health
for i in $(seq 1 25); do
    if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then break; fi
    sleep 1
done

# Login + bootstrap N printers
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/api/auth/login \
    -F username=stress-admin -F "password=$ADMIN_PW" \
    | "$PYBIN" -c 'import sys,json; print(json.load(sys.stdin).get("access_token",""))')

if [ -z "$TOKEN" ]; then
    echo "LOGIN FAILED" | tee "$CELL_DIR/FAILED"
    exit 1
fi

ODIN_ACCESS_TOKEN="$TOKEN" API_KEY="$API_KEY_VAL" PYTHONPATH=backend:. "$PYBIN" -m tests.stress.mqtt.bootstrap_printers \
    --count "$N" --base-url http://127.0.0.1:8000 --api-host 127.0.0.1 \
    --out "$CELL_DIR/bootstrap.json" >"$CELL_DIR/bootstrap.log" 2>&1

# Start monitor (with sys.modules alias for V2 import-path bug)
DATABASE_URL="sqlite:///$REPO/odin_stress.db" \
DATABASE_PATH="$REPO/odin_stress.db" \
ENCRYPTION_KEY="$ENC_KEY" \
JWT_SECRET_KEY="$JWT_KEY" \
API_KEY="$API_KEY_VAL" \
ODIN_TELEMETRY_V2=1 \
ODIN_ALLOW_INSECURE_BAMBU_BROKER=1 \
ODIN_BAMBU_INSECURE_BROKER_HOSTS='mosquitto,127.0.0.1,localhost' \
ODIN_BAMBU_INSECURE_BROKER_PORT="$MOSQ_PORT" \
ODIN_STRESS_INSTRUMENTATION="$CELL_DIR" \
ODIN_DB_PATH="$REPO/odin_stress.db" \
PYTHONPATH="$LOOPBACK_DEVICES:backend:." \
"$PYBIN" -m tests.stress.mqtt.run_monitor \
    >"$CELL_DIR/mqtt_monitor.log" 2>&1 &
echo $! > /tmp/odin-stress-monitor.pid

sleep 5  # let monitor establish all N TLS-bypass connections

# Run publisher (rate=10s matches production heartbeat throttle)
# connect-rate caps the paho client cold-connect storm — at N>=400 the test
# harness on macOS hits TCP connect_async throttling without this. 50/sec
# means N=800 finishes its connect ramp in 16s, N=1600 in 32s.
PYTHONPATH=backend:. "$PYBIN" -m tests.stress.mqtt.multi_publisher \
    --printers "$N" --duration "$DURATION" --rate 10 --connect-rate 50 \
    --broker-host 127.0.0.1 --broker-port "$MOSQ_PORT" \
    --out "$CELL_DIR/publisher.json" >"$CELL_DIR/publisher.log" 2>&1

# Render report
PYTHONPATH=backend:. "$PYBIN" -m tests.stress.mqtt.report \
    --run-dir "$CELL_DIR" >/dev/null 2>&1
