#!/usr/bin/env bash
# ============================================================
# O.D.I.N. Local Deploy — Build + Phase 0 + Pytest
#
# Usage:
#   ./ops/deploy_local.sh              # full pipeline
#   ./ops/deploy_local.sh --skip-build # skip docker build (retest only)
#   ./ops/deploy_local.sh --skip-tests # skip pytest (Phase 0 only)
#
# Run from: Mac (Docker Desktop) or any machine with Docker
# Compose:  auto-detected from script location
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
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

# --- Pre-flight ---
if ! command -v docker &>/dev/null; then
    die "Docker not found. Install Docker Desktop first."
fi

if ! docker info &>/dev/null 2>&1; then
    die "Docker daemon not running. Start Docker Desktop first."
fi

if [[ ! -f "$COMPOSE_FILE" ]]; then
    die "Compose file not found: ${COMPOSE_FILE}"
fi

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║  O.D.I.N. Local Deploy Pipeline                     ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════╝${NC}"
echo -e "  Compose dir: ${COMPOSE_DIR}"

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
    "${SCRIPT_DIR}/phase0_verify.sh" local
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

        # Run main tests (excluding RBAC — collection conflicts)
        step "Running main test suite (excluding RBAC)"
        python3 -m pytest "$TEST_DIR" -v --tb=short --ignore="${TEST_DIR}/test_rbac.py" 2>&1 | tail -30
        PYTEST_EXIT=${PIPESTATUS[0]}
        if [[ $PYTEST_EXIT -ne 0 ]]; then
            die "Main pytest suite failed with exit code ${PYTEST_EXIT}"
        fi
        ok "Main test suite passed"

        # Run RBAC tests separately (collection conflicts when run together)
        if [[ -f "${TEST_DIR}/test_rbac.py" ]]; then
            step "Running RBAC tests (separate collection)"
            python3 -m pytest "${TEST_DIR}/test_rbac.py" -v --tb=short 2>&1 | tail -30
            RBAC_EXIT=${PIPESTATUS[0]}
            if [[ $RBAC_EXIT -ne 0 ]]; then
                die "RBAC tests failed with exit code ${RBAC_EXIT}"
            fi
            ok "RBAC tests passed"
        fi
    else
        echo -e "  ${RED}⚠ No test directory found in ${COMPOSE_DIR}/{tests,test,qa}${NC}"
        echo "  Skipping pytest — run manually if needed"
    fi
else
    step "Step 3/4: Pytest SKIPPED (--skip-tests)"
fi

# --- Step 4: Summary ---
step "Step 4/4: Local Deploy Complete"
echo ""
echo -e "${GREEN}${BOLD}  ✅ Local pipeline is green. Safe to bump + push.${NC}"
echo ""
echo "  Next steps:"
echo "    make bump VERSION=X.Y.Z       # bump + commit + tag"
echo "    make release VERSION=X.Y.Z    # bump + commit + tag + push"
echo ""
