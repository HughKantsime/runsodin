#!/usr/bin/env bash
# ============================================================
# O.D.I.N. Production Deploy — Pull + Phase 0 (NEVER builds)
#
# Usage:
#   ./ops/deploy_prod.sh                 # deploy :latest
#   ./ops/deploy_prod.sh v1.0.18         # deploy specific tag
#   ./ops/deploy_prod.sh --check-only    # Phase 0 only, no deploy
#
# Run from: PRODUCTION (.71.211)
# Compose:  /opt/odin/runsodin/runsodin/docker-compose.yml
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_DIR="/opt/odin/runsodin/runsodin"
COMPOSE_FILE="${COMPOSE_DIR}/docker-compose.yml"
GHCR_IMAGE="ghcr.io/hughkantsime/odin"
DEPLOY_LOG="/opt/odin/deploy.log"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

step() { echo -e "\n${CYAN}${BOLD}▶ $1${NC}"; }
ok()   { echo -e "${GREEN}✓ $1${NC}"; }
die()  { echo -e "${RED}✗ $1${NC}"; exit 1; }

TAG="latest"
CHECK_ONLY=false

for arg in "$@"; do
    case "$arg" in
        --check-only) CHECK_ONLY=true ;;
        v*) TAG="$arg" ;;
    esac
done

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║  O.D.I.N. Production Deploy Pipeline                ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════╝${NC}"
echo -e "  ${BOLD}Tag:${NC} ${TAG}"
echo -e "  ${BOLD}Image:${NC} ${GHCR_IMAGE}:${TAG}"
echo ""

# --- Pre-flight: verify compose file is image-only ---
step "Pre-flight: Compose file safety check"

if [[ ! -f "$COMPOSE_FILE" ]]; then
    die "Compose file not found at ${COMPOSE_FILE}"
fi

ACTIVE_BUILDS=$(grep -E '^\s+build:' "$COMPOSE_FILE" | grep -v '^\s*#' || true)
if [[ -n "$ACTIVE_BUILDS" ]]; then
    die "ABORT: Production compose has active 'build:' directive!\n  Fix this before deploying.\n  Offending: ${ACTIVE_BUILDS}"
fi
ok "Compose is image-only (no build: directives)"

# --- Check-only mode ---
if [[ "$CHECK_ONLY" == true ]]; then
    step "Phase 0 — Check Only (no deploy)"
    "${SCRIPT_DIR}/phase0_verify.sh" prod
    exit $?
fi

# --- Step 1: Pull ---
step "Step 1/4: Pull image from GHCR"
docker pull "${GHCR_IMAGE}:${TAG}"
ok "Pulled ${GHCR_IMAGE}:${TAG}"

# Record digest
DIGEST=$(docker inspect "${GHCR_IMAGE}:${TAG}" --format '{{index .RepoDigests 0}}' 2>/dev/null || echo "unknown")
echo -e "  ${BOLD}Digest:${NC} ${DIGEST}"

# --- Step 2: Deploy ---
step "Step 2/4: Restart container with new image"

# If using a specific tag, update compose file
if [[ "$TAG" != "latest" ]]; then
    # Swap image tag in compose (sed is fine here — it's a simple controlled substitution)
    sed -i "s|image: ${GHCR_IMAGE}:.*|image: ${GHCR_IMAGE}:${TAG}|" "$COMPOSE_FILE"
    ok "Updated compose to use tag: ${TAG}"
fi

cd "$COMPOSE_DIR"
docker compose down
docker compose up -d
ok "Container restarted"

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
    die "Container did not become healthy within timeout — ROLLBACK NEEDED"
fi

# --- Step 3: Phase 0 ---
step "Step 3/4: Phase 0 — Post-deploy verification"
"${SCRIPT_DIR}/phase0_verify.sh" prod
ok "Phase 0 passed"

# --- Step 4: Log + Summary ---
step "Step 4/4: Deploy Complete"

# Append to deploy log
CONTAINER_VERSION=$(docker exec odin cat /app/VERSION 2>/dev/null || echo "unknown")
LOG_ENTRY="$(date -u '+%Y-%m-%dT%H:%M:%SZ') | tag=${TAG} | version=${CONTAINER_VERSION} | digest=${DIGEST}"
echo "$LOG_ENTRY" >> "$DEPLOY_LOG"
ok "Logged to ${DEPLOY_LOG}"

echo ""
echo -e "${GREEN}${BOLD}  ✅ Production deploy complete${NC}"
echo ""
echo "  Deployed:"
echo "    Image:   ${GHCR_IMAGE}:${TAG}"
echo "    Version: ${CONTAINER_VERSION}"
echo "    Digest:  ${DIGEST}"
echo ""
echo -e "  ${YELLOW}Verify in browser: http://<prod-ip>:8000${NC}"
echo ""
