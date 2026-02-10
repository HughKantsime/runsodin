#!/usr/bin/env bash
# ============================================================
# O.D.I.N. Sandbox Deploy — Build + Phase 0 + Pytest
#
# Usage:
#   ./ops/deploy_sandbox.sh              # full pipeline
#   ./ops/deploy_sandbox.sh --skip-build # skip docker build (retest only)
#   ./ops/deploy_sandbox.sh --skip-tests # skip pytest (Phase 0 only)
#
# Run from: SANDBOX (.70.200)
# Compose:  /opt/printfarm-scheduler/docker-compose.yml
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_DIR="/opt/printfarm-scheduler"
COMPOSE_FILE="${COMPOSE_DIR}/docker-compose.yml"

RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

step() { echo -e "\n${CYAN}${BOLD}▶ $1${NC}"; }
ok()   { echo -e "${GREEN}✓ $1${NC}"; }
die()  { echo -e "${RED}✗ $1${NC}"; exit 1; }

SKIP_BUILD=false
SKIP_TESTS=false

for arg in "$@"; do
    case "$arg" in
        --skip-build) SKIP_BUILD=true ;;
        --skip-tests) SKIP_TESTS=true ;;
    esac
done

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║  O.D.I.N. Sandbox Deploy Pipeline                   ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════╝${NC}"

# --- Step 1: Build ---
if [[ "$SKIP_BUILD" == false ]]; then
    step "Step 1/4: Docker build + restart"
    cd "$COMPOSE_DIR"
    docker compose down
    docker compose build --no-cache
    docker compose up -d
    ok "Container rebuilt and started"

    step "Waiting for container health..."
    RETRIES=20
    while [[ $RETRIES -gt 0 ]]; do
        HEALTH=$(docker inspect odin --format '{{.State.Health.Status}}' 2>/dev/null || echo "starting")
        if [[ "$HEALTH" == "healthy" ]]; then
            ok "Container is healthy"
            break
        fi
        echo "  Waiting... (${HEALTH})"
        sleep 5
        ((RETRIES--))
    done
    if [[ "$HEALTH" != "healthy" ]]; then
        die "Container did not become healthy within timeout"
    fi
else
    step "Step 1/4: Build SKIPPED (--skip-build)"
fi

# --- Step 2: Phase 0 ---
step "Step 2/4: Phase 0 — Docker Sanity"
if [[ -x "${SCRIPT_DIR}/phase0_verify.sh" ]]; then
    "${SCRIPT_DIR}/phase0_verify.sh" sandbox
else
    die "phase0_verify.sh not found or not executable at ${SCRIPT_DIR}/phase0_verify.sh"
fi
ok "Phase 0 passed"

# --- Step 3: Pytest ---
if [[ "$SKIP_TESTS" == false ]]; then
    step "Step 3/4: Phase 1-3 Pytest Suite"

    # Find the test directory
    TEST_DIR=""
    for candidate in "${COMPOSE_DIR}/tests" "${COMPOSE_DIR}/test" "${COMPOSE_DIR}/qa"; do
        if [[ -d "$candidate" ]]; then
            TEST_DIR="$candidate"
            break
        fi
    done

    if [[ -n "$TEST_DIR" ]]; then
        cd "$COMPOSE_DIR"
        # Run pytest against the running container
        # Adjust the pytest command to match your setup
        python3 -m pytest "$TEST_DIR" -v --tb=short 2>&1 | tail -30
        PYTEST_EXIT=${PIPESTATUS[0]}
        if [[ $PYTEST_EXIT -eq 0 ]]; then
            ok "All pytest phases passed"
        else
            die "Pytest failed with exit code ${PYTEST_EXIT}"
        fi
    else
        echo -e "  ${RED}⚠ No test directory found in ${COMPOSE_DIR}/{tests,test,qa}${NC}"
        echo "  Skipping pytest — run manually if needed"
    fi
else
    step "Step 3/4: Pytest SKIPPED (--skip-tests)"
fi

# --- Step 4: Summary ---
step "Step 4/4: Sandbox Deploy Complete"
echo ""
echo -e "${GREEN}${BOLD}  ✅ Sandbox is green. Safe to tag + push to GHCR.${NC}"
echo ""
echo "  Next steps:"
echo "    1. git tag v1.0.XX && git push origin v1.0.XX"
echo "    2. Wait for GHCR workflow to complete"
echo "    3. Run: ./ops/deploy_prod.sh v1.0.XX"
echo ""
