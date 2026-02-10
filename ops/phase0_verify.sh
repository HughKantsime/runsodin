#!/usr/bin/env bash
# ============================================================
# O.D.I.N. Phase 0 — Docker Deployability & Runtime Sanity
#
# Usage:
#   ./ops/phase0_verify.sh              # auto-detect environment
#   ./ops/phase0_verify.sh sandbox      # force sandbox mode
#   ./ops/phase0_verify.sh prod         # force production mode
#
# Exit codes:
#   0 = all checks passed
#   1 = one or more checks failed
#
# Designed to run on both SANDBOX (.70.200) and PROD (.71.211)
# ============================================================

set -euo pipefail

# --- Colors & output helpers ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

PASS_COUNT=0
FAIL_COUNT=0
WARN_COUNT=0

pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo -e "  ${GREEN}✓ PASS${NC}  $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo -e "  ${RED}✗ FAIL${NC}  $1"; }
warn() { WARN_COUNT=$((WARN_COUNT + 1)); echo -e "  ${YELLOW}⚠ WARN${NC}  $1"; }
header() { echo -e "\n${CYAN}${BOLD}━━━ $1 ━━━${NC}"; }
info() { echo -e "  ${BOLD}→${NC} $1"; }

# --- Configuration ---
CONTAINER_NAME="odin"
API_PORT="8000"
API_BASE="http://localhost:${API_PORT}"
EXPECTED_GHCR_IMAGE="ghcr.io/hughkantsime/odin"

# Supervisor services that MUST be running
# All monitors now stay alive (sleep+retry when no printers configured)
REQUIRED_SERVICES=("backend" "mqtt_monitor" "go2rtc" "elegoo_monitor" "moonraker_monitor" "prusalink_monitor")

# API endpoints to check (method path expected_status)
API_CHECKS=(
    "GET /health 200"
    "GET /api/config 200"
    "GET /api/printers 200"
    "GET /api/jobs 200"
)

# --- Detect environment ---
detect_environment() {
    local forced="${1:-auto}"

    if [[ "$forced" != "auto" ]]; then
        ENV_MODE="$forced"
    else
        # Auto-detect based on hostname or IP
        local hostname
        hostname=$(hostname)
        if [[ "$hostname" == *"sandbox"* ]] || ip addr show 2>/dev/null | grep -q "70\.200"; then
            ENV_MODE="sandbox"
        elif [[ "$hostname" == *"odin"* ]] || ip addr show 2>/dev/null | grep -q "71\.211"; then
            ENV_MODE="prod"
        else
            ENV_MODE="unknown"
        fi
    fi

    echo -e "${BOLD}Environment: ${CYAN}${ENV_MODE^^}${NC}"
}

# --- Locate compose file ---
find_compose_file() {
    local search_paths=(
        "/opt/odin/runsodin/runsodin/docker-compose.yml"
        "/opt/odin/docker-compose.yml"
        "/opt/printfarm-scheduler/docker-compose.yml"
        "./docker-compose.yml"
    )

    for p in "${search_paths[@]}"; do
        if [[ -f "$p" ]]; then
            COMPOSE_FILE="$p"
            return 0
        fi
    done

    COMPOSE_FILE=""
    return 1
}

# ============================================================
# Phase 0A — Provenance: "Am I running the right image?"
# ============================================================
phase_0a() {
    header "Phase 0A — Provenance"

    # Find compose file
    if find_compose_file; then
        info "Compose file: ${COMPOSE_FILE}"
    else
        fail "No compose file found in expected locations"
        return
    fi

    # Check build: vs image: in compose — look for uncommented build: lines
    local uncommented_build
    uncommented_build=$(grep -E '^\s+build:' "$COMPOSE_FILE" | grep -v '^\s*#' || true)
    if [[ -n "$uncommented_build" ]]; then
        if [[ "$ENV_MODE" == "prod" ]]; then
            fail "PRODUCTION compose has active 'build:' — this MUST be image-only"
            info "Offending line: ${uncommented_build}"
        else
            pass "Sandbox compose uses 'build:' (expected for dev)"
        fi
    else
        pass "No active 'build:' lines in compose"
    fi

    # Check image: line exists (prod requirement)
    if [[ "$ENV_MODE" == "prod" ]]; then
        local image_line
        image_line=$(grep -E '^\s+image:' "$COMPOSE_FILE" | grep -v '^\s*#' | head -1 || true)
        if [[ -n "$image_line" ]]; then
            pass "Compose uses image reference: $(echo "$image_line" | xargs)"
            if echo "$image_line" | grep -q "$EXPECTED_GHCR_IMAGE"; then
                pass "Image points to GHCR (${EXPECTED_GHCR_IMAGE})"
            else
                fail "Image does NOT point to expected GHCR repo"
            fi
        else
            fail "No active 'image:' line found in prod compose"
        fi
    fi

    # Check running container's image
    if docker ps --filter "name=${CONTAINER_NAME}" --format "{{.Names}}" | grep -q "${CONTAINER_NAME}"; then
        local running_image
        running_image=$(docker inspect "${CONTAINER_NAME}" --format '{{.Config.Image}}' 2>/dev/null || echo "unknown")
        local image_id
        image_id=$(docker inspect "${CONTAINER_NAME}" --format '{{.Image}}' 2>/dev/null || echo "unknown")

        info "Running image: ${running_image}"
        info "Image ID: ${image_id}"

        if [[ "$ENV_MODE" == "prod" ]]; then
            if echo "$running_image" | grep -q "$EXPECTED_GHCR_IMAGE"; then
                pass "Container is running GHCR image"
            else
                fail "Container is NOT running GHCR image (got: ${running_image})"
            fi
        fi
    else
        fail "Container '${CONTAINER_NAME}' is not running"
        return
    fi

    # Check VERSION file inside container
    local container_version
    container_version=$(docker exec "${CONTAINER_NAME}" cat /app/VERSION 2>/dev/null || echo "NOT_FOUND")
    if [[ "$container_version" != "NOT_FOUND" ]]; then
        info "Container VERSION: ${container_version}"
        pass "VERSION file present in container"
    else
        warn "No VERSION file found in container at /app/VERSION"
    fi
}

# ============================================================
# Phase 0B — Container Health + Process Health
# ============================================================
phase_0b() {
    header "Phase 0B — Container & Process Health"

    # Container running?
    local status
    status=$(docker inspect "${CONTAINER_NAME}" --format '{{.State.Status}}' 2>/dev/null || echo "not_found")
    if [[ "$status" == "running" ]]; then
        pass "Container '${CONTAINER_NAME}' is running"
    else
        fail "Container '${CONTAINER_NAME}' status: ${status}"
        return
    fi

    # Docker healthcheck status
    local health
    health=$(docker inspect "${CONTAINER_NAME}" --format '{{.State.Health.Status}}' 2>/dev/null || echo "none")
    if [[ "$health" == "healthy" ]]; then
        pass "Docker healthcheck: healthy"
    elif [[ "$health" == "none" ]]; then
        warn "No Docker healthcheck configured"
    else
        fail "Docker healthcheck: ${health}"
    fi

    # Restart count
    local restarts
    restarts=$(docker inspect "${CONTAINER_NAME}" --format '{{.RestartCount}}' 2>/dev/null || echo "-1")
    if [[ "$restarts" -eq 0 ]]; then
        pass "Restart count: 0"
    elif [[ "$restarts" -lt 3 ]]; then
        warn "Restart count: ${restarts} (investigate if recurring)"
    else
        fail "Restart count: ${restarts} — possible crash loop"
    fi

    # Supervisor status — required services
    for svc in "${REQUIRED_SERVICES[@]}"; do
        local svc_status
        svc_status=$(docker exec "${CONTAINER_NAME}" supervisorctl status "$svc" 2>/dev/null | awk '{print $2}' || echo "UNKNOWN")
        if [[ "$svc_status" == "RUNNING" ]]; then
            pass "Supervisor: ${svc} is RUNNING"
        elif [[ "$svc_status" == "FATAL" ]]; then
            fail "Supervisor: ${svc} is FATAL"
        elif [[ "$svc_status" == "EXITED" ]]; then
            fail "Supervisor: ${svc} is EXITED (required service)"
        else
            fail "Supervisor: ${svc} status: ${svc_status}"
        fi
    done
}

# ============================================================
# Phase 0C — API Sanity (zero 500 tolerance)
# ============================================================
phase_0c() {
    header "Phase 0C — API Sanity"

    # Wait for API to be responsive (up to 15s)
    local retries=5
    while [[ $retries -gt 0 ]]; do
        if curl -sS -o /dev/null -w "" "${API_BASE}/health" 2>/dev/null; then
            break
        fi
        sleep 3
        ((retries--))
    done

    for check in "${API_CHECKS[@]}"; do
        local method path expected
        read -r method path expected <<< "$check"
        local url="${API_BASE}${path}"
        local http_code
        http_code=$(curl -sS -o /dev/null -w "%{http_code}" -X "$method" "$url" 2>/dev/null || echo "000")

        if [[ "$http_code" == "$expected" ]]; then
            pass "${method} ${path} → ${http_code}"
        elif [[ "$http_code" == "500" ]]; then
            fail "${method} ${path} → 500 (server error — zero tolerance)"
        elif [[ "$http_code" == "000" ]]; then
            fail "${method} ${path} → connection refused"
        else
            warn "${method} ${path} → ${http_code} (expected ${expected})"
        fi
    done
}

# ============================================================
# Phase 0D — Configuration Sanity
# ============================================================
phase_0d() {
    header "Phase 0D — Configuration Sanity"

    # Check required env vars inside container
    # Note: using 'env | grep' instead of 'printenv' to avoid subshell quoting issues
    local required_vars=("ENCRYPTION_KEY" "JWT_SECRET_KEY" "DATABASE_URL")
    local container_env
    container_env=$(docker exec "${CONTAINER_NAME}" env 2>/dev/null || echo "")

    for var in "${required_vars[@]}"; do
        local val
        val=$(echo "$container_env" | sed -n "s/^${var}=//p")
        if [[ -n "$val" ]]; then
            pass "Env var ${var} is set"
        else
            fail "Env var ${var} is MISSING or empty"
        fi
    done

    # Check DB connectivity (can the app read the DB?)
    local db_check
    db_check=$(curl -sS -o /dev/null -w "%{http_code}" "${API_BASE}/api/config" 2>/dev/null || echo "000")
    if [[ "$db_check" == "200" ]]; then
        pass "DB connectivity OK (api/config returns 200)"
    else
        fail "DB connectivity issue (api/config returned ${db_check})"
    fi
}

# ============================================================
# Phase 0E — "No rebuild on prod" guardrail
# ============================================================
phase_0e() {
    header "Phase 0E — Production Guardrail"

    if [[ "$ENV_MODE" != "prod" ]]; then
        info "Skipping prod guardrail checks (environment: ${ENV_MODE})"
        return
    fi

    if [[ -z "${COMPOSE_FILE:-}" ]]; then
        fail "Cannot check guardrails — no compose file found"
        return
    fi

    # Hard fail if build: is active on prod
    local active_builds
    active_builds=$(grep -E '^\s+build:' "$COMPOSE_FILE" | grep -v '^\s*#' || true)
    if [[ -n "$active_builds" ]]; then
        fail "CRITICAL: Production compose has active 'build:' directive!"
        fail "This means 'docker compose up' will BUILD locally instead of pulling from GHCR."
        fail "Fix: comment out or remove 'build:' and ensure 'image:' is active."
        info "Offending line(s):"
        echo "$active_builds" | while read -r line; do
            echo -e "    ${RED}${line}${NC}"
        done
    else
        pass "No active 'build:' in production compose"
    fi

    # Verify image tag isn't purely :latest (warn, not fail)
    local image_tag
    image_tag=$(grep -E '^\s+image:' "$COMPOSE_FILE" | grep -v '^\s*#' | head -1 | sed 's/.*://' || true)
    if [[ "$image_tag" == "latest" ]]; then
        warn "Using :latest tag — consider pinning to a specific version (e.g., v1.0.18)"
    else
        pass "Image tag is pinned: ${image_tag}"
    fi
}

# ============================================================
# Summary
# ============================================================
summary() {
    header "Summary"
    echo -e "  ${GREEN}Passed: ${PASS_COUNT}${NC}"
    echo -e "  ${RED}Failed: ${FAIL_COUNT}${NC}"
    echo -e "  ${YELLOW}Warnings: ${WARN_COUNT}${NC}"
    echo ""

    if [[ $FAIL_COUNT -gt 0 ]]; then
        echo -e "  ${RED}${BOLD}⛔ Phase 0 FAILED — DO NOT PROCEED${NC}"
        exit 1
    elif [[ $WARN_COUNT -gt 0 ]]; then
        echo -e "  ${YELLOW}${BOLD}⚠️  Phase 0 PASSED with warnings${NC}"
        exit 0
    else
        echo -e "  ${GREEN}${BOLD}✅ Phase 0 PASSED — all clear${NC}"
        exit 0
    fi
}

# ============================================================
# Phase 0F — Auth Smoke (login round-trip)
# ============================================================
phase_0f() {
    header "Phase 0F — Auth Smoke"

    # Credentials from env or defaults
    local admin_user="${ODIN_ADMIN_USER:-admin}"
    local admin_pass="${ODIN_ADMIN_PASSWORD:-}"

    if [[ -z "$admin_pass" ]]; then
        warn "ODIN_ADMIN_PASSWORD not set — skipping auth smoke"
        info "Set ODIN_ADMIN_PASSWORD env var to enable (or export before running)"
        return
    fi

    # Attempt login (form-encoded, OAuth2 password flow)
    local login_response
    login_response=$(curl -sS -w "\n%{http_code}" -X POST "${API_BASE}/api/auth/login" \
        -d "username=${admin_user}&password=${admin_pass}" 2>/dev/null || echo -e "\n000")

    local login_body
    login_body=$(echo "$login_response" | head -n -1)
    local login_code
    login_code=$(echo "$login_response" | tail -1)

    if [[ "$login_code" == "200" ]]; then
        pass "Login POST /api/auth/login → 200"
    else
        fail "Login POST /api/auth/login → ${login_code}"
        return
    fi

    # Extract token from response
    local token
    token=$(echo "$login_body" | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null || echo "")

    if [[ -z "$token" ]]; then
        # Try alternate key names
        token=$(echo "$login_body" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('token','') or d.get('jwt',''))" 2>/dev/null || echo "")
    fi

    if [[ -n "$token" ]]; then
        pass "JWT token received"
    else
        fail "No JWT token in login response"
        return
    fi

    # Use token to hit a protected endpoint
    local auth_check
    auth_check=$(curl -sS -o /dev/null -w "%{http_code}" -H "Authorization: Bearer ${token}" \
        "${API_BASE}/api/auth/me" 2>/dev/null || echo "000")

    if [[ "$auth_check" == "200" ]]; then
        pass "Authenticated GET /api/auth/me → 200"
    else
        fail "Authenticated GET /api/auth/me → ${auth_check}"
    fi

    # Store token for Phase 0G
    AUTH_TOKEN="$token"
}

# ============================================================
# Phase 0G — DB Write Probe (create backup → verify in list)
# ============================================================
phase_0g() {
    header "Phase 0G — DB Write Probe"

    if [[ -z "${AUTH_TOKEN:-}" ]]; then
        warn "No auth token available — skipping DB write probe"
        info "Phase 0F must pass with credentials to enable this check"
        return
    fi

    local auth_header="Authorization: Bearer ${AUTH_TOKEN}"

    # Create a backup (proves DB can be read + filesystem can be written)
    local create_code
    create_code=$(curl -sS -o /dev/null -w "%{http_code}" -X POST "${API_BASE}/api/backups" \
        -H "$auth_header" 2>/dev/null || echo "000")

    if [[ "$create_code" == "200" || "$create_code" == "201" ]]; then
        pass "DB write probe: POST /api/backups → ${create_code}"
    else
        fail "DB write probe: POST /api/backups → ${create_code}"
        return
    fi

    # Verify backup appears in list
    local list_code
    list_code=$(curl -sS -o /dev/null -w "%{http_code}" -H "$auth_header" \
        "${API_BASE}/api/backups" 2>/dev/null || echo "000")

    if [[ "$list_code" == "200" ]]; then
        pass "DB read probe: GET /api/backups → 200"
    else
        fail "DB read probe: GET /api/backups → ${list_code}"
    fi
}

# ============================================================
# Main
# ============================================================
AUTH_TOKEN=""

main() {
    echo ""
    echo -e "${BOLD}╔══════════════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}║  O.D.I.N. Phase 0 — Docker Deployability Check     ║${NC}"
    echo -e "${BOLD}╚══════════════════════════════════════════════════════╝${NC}"
    echo ""

    detect_environment "${1:-auto}"
    echo ""

    phase_0a
    phase_0b
    phase_0c
    phase_0d
    phase_0e
    phase_0f
    phase_0g
    summary
}

main "$@"
