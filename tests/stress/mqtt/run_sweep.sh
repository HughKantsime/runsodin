#!/usr/bin/env bash
# Stress sweep across (printer count) × (db backend) × (telemetry v2 flag).
#
# Prereqs:
#   - mosquitto reachable at localhost:1883 (e.g. via `docker compose -f
#     ops/demo/docker-compose.demo.yml up -d mosquitto`)
#   - postgres reachable at the URL passed in --pg-url, OR omit --pg-url to
#     skip the postgres half of the matrix.
#   - paho-mqtt installed in the active venv: `pip install paho-mqtt`
#   - ODIN backend NOT already running on :8000 — this script starts/stops it
#     per cell so the env vars take effect.
#
# Usage:
#   tests/stress/mqtt/run_sweep.sh \
#     --counts "10 25 50" \
#     --pg-url "postgresql://odin:odin@localhost:5432/odin_stress" \
#     --duration 600
#
# Output: stress-out/sweep-<ts>/<cell>/{REPORT.md,latency.jsonl,...}
#         stress-out/sweep-<ts>/SWEEP.md (rolled up across cells)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT"

COUNTS="10 25 50"
PG_URL=""
DURATION=300
RATE=10
V2_FLAGS="0 1"
FIXTURE="tests/fixtures/telemetry/bambu-x1c-ams-swap.jsonl"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --counts) COUNTS="$2"; shift 2;;
        --pg-url) PG_URL="$2"; shift 2;;
        --duration) DURATION="$2"; shift 2;;
        --rate) RATE="$2"; shift 2;;
        --v2-flags) V2_FLAGS="$2"; shift 2;;
        --fixture) FIXTURE="$2"; shift 2;;
        *) echo "unknown arg: $1" >&2; exit 2;;
    esac
done

TS="$(date -u +%Y%m%dT%H%M%SZ)"
SWEEP_DIR="stress-out/sweep-${TS}"
mkdir -p "$SWEEP_DIR"

# Build the list of (count, db_url, db_label, v2) tuples
DB_TUPLES=()
DB_TUPLES+=("sqlite:///odin_stress.db|sqlite")
if [[ -n "$PG_URL" ]]; then
    DB_TUPLES+=("$PG_URL|postgres")
fi

run_cell() {
    local count="$1" db_url="$2" db_label="$3" v2="$4"
    local cell="n${count}-${db_label}-v2${v2}"
    local cell_dir="${SWEEP_DIR}/${cell}"
    mkdir -p "$cell_dir"
    echo ""
    echo "=== cell: $cell ==="

    # Wipe the sqlite DB between runs so row counts are comparable.
    if [[ "$db_label" == "sqlite" ]]; then
        rm -f odin_stress.db odin_stress.db-wal odin_stress.db-shm
    fi

    # Boot backend
    DATABASE_URL="$db_url" \
        ODIN_TELEMETRY_V2="$v2" \
        ODIN_STRESS_INSTRUMENTATION="$cell_dir" \
        ODIN_DB_PATH="odin_stress.db" \
        API_KEY="capture-pipeline-local" \
        ODIN_DEMO_API_KEY="capture-pipeline-local" \
        ODIN_ALLOW_INSECURE_BAMBU_BROKER=1 \
        ODIN_BAMBU_INSECURE_BROKER_HOSTS="mosquitto,127.0.0.1,localhost" \
        python -m uvicorn backend.core.app:app --host 127.0.0.1 --port 8000 \
        > "${cell_dir}/backend.log" 2>&1 &
    local backend_pid=$!
    echo "backend pid=$backend_pid"

    # Wait for /health
    for i in $(seq 1 30); do
        if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
            echo "backend up after ${i}s"
            break
        fi
        sleep 1
    done

    # Bootstrap printers
    python -m tests.stress.mqtt.bootstrap_printers \
        --count "$count" \
        --base-url http://127.0.0.1:8000 \
        --out "${cell_dir}/bootstrap.json" \
        || echo "bootstrap reported partial failure — continuing"

    # Run publisher
    python -m tests.stress.mqtt.multi_publisher \
        --printers "$count" \
        --fixture "$FIXTURE" \
        --rate "$RATE" \
        --duration "$DURATION" \
        --out "${cell_dir}/publisher.json"

    # Stop backend gracefully
    kill -TERM "$backend_pid" 2>/dev/null || true
    wait "$backend_pid" 2>/dev/null || true

    # Render cell report
    python -m tests.stress.mqtt.report --run-dir "$cell_dir"
}

for count in $COUNTS; do
    for tuple in "${DB_TUPLES[@]}"; do
        IFS='|' read -r db_url db_label <<<"$tuple"
        for v2 in $V2_FLAGS; do
            run_cell "$count" "$db_url" "$db_label" "$v2"
        done
    done
done

# Roll up
{
    echo "# Sweep ${TS}"
    echo ""
    echo "Rate: ${RATE}s  Duration: ${DURATION}s  Fixture: ${FIXTURE}"
    echo ""
    for cell_dir in "${SWEEP_DIR}"/n*; do
        echo ""
        echo "---"
        cat "${cell_dir}/REPORT.md"
    done
} > "${SWEEP_DIR}/SWEEP.md"

echo ""
echo "sweep complete — see ${SWEEP_DIR}/SWEEP.md"
