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
mkdir -p "$CELL_DIR"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO"

ENC_KEY="$(cat /tmp/odin-stress-enc-key)"
PYBIN="$REPO/.venv-stress/bin/python3"

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

# Migrations: ORM create_all FIRST (so system_config etc. exist), then SQL migrations.
DATABASE_URL="sqlite:///$REPO/odin_stress.db" \
DATABASE_PATH="$REPO/odin_stress.db" \
PYTHONPATH=backend "$PYBIN" -c "
import importlib
from pathlib import Path
# Force-load ORM models so Base.metadata is populated before create_all
# Mirror entrypoint.sh exactly: import core.models first (SystemConfig), then
# module models in the same order as the prod entrypoint.
import core.models
for mod_path in [
    'modules.printers.models',
    'modules.jobs.models',
    'modules.inventory.models',
    'modules.models_library.models',
    'modules.vision.models',
    'modules.notifications.models',
    'modules.orders.models',
    'modules.archives.models',
    'modules.system.models',
]:
    try: importlib.import_module(mod_path)
    except Exception as e: print(f'skip {mod_path}: {e}')
from core.base import Base
from core.db import engine, run_core_migrations, run_module_migrations
Base.metadata.create_all(bind=engine)
run_core_migrations()
run_module_migrations(Path('backend/modules'))
import sqlite3
c = sqlite3.connect('odin_stress.db')
# Force-run module migrations that didn't auto-trigger
for module in ['printers', 'jobs', 'archives']:
    p = f'backend/modules/{module}/migrations/001_initial.sql'
    try: c.executescript(open(p).read()); c.commit()
    except FileNotFoundError: pass
    except Exception as e: pass
from passlib.context import CryptContext
hash_ = CryptContext(schemes=['bcrypt']).hash('stress-admin-pw-2026')
c.execute('INSERT OR IGNORE INTO users (username, password_hash, role, is_active, mfa_enabled) VALUES (?, ?, ?, ?, ?)',
          ('stress-admin', hash_, 'admin', 1, 0))
c.commit()
print('migrate done')
" >"$CELL_DIR/migrate.log" 2>&1

# Start backend
DATABASE_URL="sqlite:///$REPO/odin_stress.db" \
DATABASE_PATH="$REPO/odin_stress.db" \
ENCRYPTION_KEY="$ENC_KEY" \
JWT_SECRET_KEY='stress-jwt-key-not-for-prod-12345678901234567890' \
API_KEY='capture-pipeline-local' \
ODIN_TELEMETRY_V2=1 \
ODIN_ALLOW_INSECURE_BAMBU_BROKER=1 \
ODIN_BAMBU_INSECURE_BROKER_HOSTS='mosquitto,127.0.0.1,localhost' \
ODIN_STRESS_INSTRUMENTATION="$CELL_DIR" \
ODIN_DB_PATH="$REPO/odin_stress.db" \
PYTHONPATH=backend \
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
    -F username=stress-admin -F password=stress-admin-pw-2026 \
    | "$PYBIN" -c 'import sys,json; print(json.load(sys.stdin).get("access_token",""))')

if [ -z "$TOKEN" ]; then
    echo "LOGIN FAILED" | tee "$CELL_DIR/FAILED"
    exit 1
fi

ODIN_ACCESS_TOKEN="$TOKEN" PYTHONPATH=backend:. "$PYBIN" -m tests.stress.mqtt.bootstrap_printers \
    --count "$N" --base-url http://127.0.0.1:8000 --api-host mosquitto \
    --out "$CELL_DIR/bootstrap.json" >"$CELL_DIR/bootstrap.log" 2>&1

# Start monitor (with sys.modules alias for V2 import-path bug)
DATABASE_URL="sqlite:///$REPO/odin_stress.db" \
DATABASE_PATH="$REPO/odin_stress.db" \
ENCRYPTION_KEY="$ENC_KEY" \
JWT_SECRET_KEY='stress-jwt-key-not-for-prod-12345678901234567890' \
API_KEY='capture-pipeline-local' \
ODIN_TELEMETRY_V2=1 \
ODIN_ALLOW_INSECURE_BAMBU_BROKER=1 \
ODIN_BAMBU_INSECURE_BROKER_HOSTS='mosquitto,127.0.0.1,localhost' \
ODIN_STRESS_INSTRUMENTATION="$CELL_DIR" \
ODIN_DB_PATH="$REPO/odin_stress.db" \
PYTHONPATH=backend:. \
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
    --broker-host 127.0.0.1 --broker-port 1883 \
    --out "$CELL_DIR/publisher.json" >"$CELL_DIR/publisher.log" 2>&1

# Render report
PYTHONPATH=backend:. "$PYBIN" -m tests.stress.mqtt.report \
    --run-dir "$CELL_DIR" >/dev/null 2>&1
