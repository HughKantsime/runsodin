#!/usr/bin/env bash
# O.D.I.N. Updater — run from your odin/ install directory
set -euo pipefail

ODIN_IMAGE="ghcr.io/hughkantsime/odin:latest"
ODIN_REPO="https://raw.githubusercontent.com/HughKantsime/runsodin/master"
FORCE=false

for arg in "$@"; do
    case "$arg" in
        --force|-f) FORCE=true ;;
    esac
done

# ─── Display Library ───────────────────────────────────────────────────────────

SPINNER_PID=""

if [ -t 1 ] && [ -t 2 ]; then
    IS_TTY=true
    CYAN=$'\033[36m'   BOLD=$'\033[1m'    GREEN=$'\033[32m'
    RED=$'\033[31m'    YELLOW=$'\033[33m'  DIM=$'\033[2m'
    RESET=$'\033[0m'
else
    IS_TTY=false
    CYAN="" BOLD="" GREEN="" RED="" YELLOW="" DIM="" RESET=""
fi

ok()   { printf "  ${GREEN}✓${RESET} %s\n" "$*"; }
err()  { printf "  ${RED}✗${RESET} %s\n" "$*" >&2; }
warn() { printf "  ${YELLOW}!${RESET} %s\n" "$*"; }
dim()  { printf "    ${DIM}%s${RESET}\n" "$*"; }

spin_start() {
    if [ "$IS_TTY" = true ]; then
        local msg="$1"
        (
            local frames=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')
            local i=0
            while true; do
                printf "\r  ${CYAN}%s${RESET} %s" "${frames[$i]}" "$msg"
                i=$(( (i + 1) % ${#frames[@]} ))
                sleep 0.08
            done
        ) &
        SPINNER_PID=$!
        disown "$SPINNER_PID" 2>/dev/null
    fi
}

spin_stop() {
    if [ -n "$SPINNER_PID" ]; then
        kill "$SPINNER_PID" 2>/dev/null || true
        wait "$SPINNER_PID" 2>/dev/null || true
        SPINNER_PID=""
        printf "\r\033[K"
    fi
}

phase() {
    local num="$1" total="$2" name="$3"
    printf "\n${BOLD}[%s/%s] %s${RESET}\n" "$num" "$total" "$name"
}

draw_box() {
    local title="$1"; shift
    local lines=("$@")
    local max_len=${#title}

    for line in "${lines[@]}"; do
        local key="${line%%|*}"
        local val="${line#*|}"
        local len=$(( ${#key} + ${#val} + 6 ))
        (( len > max_len )) && max_len=$len
    done

    local width=$(( max_len + 4 ))
    local bar=""
    for (( i = 0; i < width; i++ )); do bar+="═"; done

    printf "\n${CYAN}╔%s╗${RESET}\n" "$bar"
    local pad=$(( width - ${#title} ))
    printf "${CYAN}║${RESET}  ${BOLD}%s${RESET}%*s${CYAN}║${RESET}\n" "$title" "$pad" ""
    printf "${CYAN}╠%s╣${RESET}\n" "$bar"
    printf "${CYAN}║${RESET}%*s${CYAN}║${RESET}\n" "$(( width + 2 ))" ""

    for line in "${lines[@]}"; do
        local key="${line%%|*}"
        local val="${line#*|}"
        local content="  ${key}  ${val}"
        local pad=$(( width - ${#content} ))
        printf "${CYAN}║${RESET}  ${DIM}%-10s${RESET} %s%*s${CYAN}║${RESET}\n" "$key" "$val" "$(( pad + 2 ))" ""
    done

    printf "${CYAN}║${RESET}%*s${CYAN}║${RESET}\n" "$(( width + 2 ))" ""
    printf "${CYAN}╚%s╝${RESET}\n" "$bar"
}

cleanup() {
    spin_stop
    if [ "${UPDATE_FAILED:-}" = "1" ]; then
        printf "\n${RED}Update failed.${RESET}\n"
        dim "If the problem persists, open an issue at:"
        dim "https://github.com/HughKantsime/runsodin/issues"
    fi
}

trap cleanup EXIT

die() {
    spin_stop
    err "$1"
    [ -n "${2:-}" ] && dim "$2"
    [ -n "${3:-}" ] && dim "$3"
    UPDATE_FAILED=1
    exit 1
}

detect_ip() {
    # Read from .env first
    if [ -f .env ]; then
        local env_ip
        env_ip=$(grep -E '^ODIN_HOST_IP=' .env 2>/dev/null | cut -d= -f2 | tr -d '"' || true)
        if [ -n "$env_ip" ]; then
            echo "$env_ip"
            return
        fi
    fi
    # Auto-detect
    local ip=""
    if command -v hostname &>/dev/null; then
        ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    fi
    if [ -z "$ip" ] && command -v ip &>/dev/null; then
        ip=$(ip route get 1.1.1.1 2>/dev/null | awk '/src/ {for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}')
    fi
    echo "${ip:-localhost}"
}

# ─── Banner ────────────────────────────────────────────────────────────────────

banner() {
    printf "${CYAN}${BOLD}"
    cat << 'ART'

     ██████╗ ██████╗ ██╗███╗   ██╗
    ██╔═══██╗██╔══██╗██║████╗  ██║
    ██║   ██║██║  ██║██║██╔██╗ ██║
    ██║   ██║██║  ██║██║██║╚██╗██║
    ╚██████╔╝██████╔╝██║██║ ╚████║
     ╚═════╝ ╚═════╝ ╚═╝╚═╝  ╚═══╝

ART
    printf "${RESET}"
    printf "    ${DIM}Updater — Orchestrated Dispatch & Inventory Network${RESET}\n"
}

# ─── Main ──────────────────────────────────────────────────────────────────────

cd "$(dirname "$0")"

START_TIME=$(date +%s)

banner

# Verify we're in the right directory
if [ ! -f docker-compose.yml ]; then
    die "docker-compose.yml not found in $(pwd)" \
        "Run this script from your odin/ install directory."
fi

TOTAL=6

# ── Phase 1: Self-update ──────────────────────────────────────────────────────

if [ "${ODIN_SELF_UPDATED:-}" != "1" ]; then
    spin_start "Checking for updater updates..."
    tmp_update=$(mktemp)
    if curl -sfL "${ODIN_REPO}/install/update.sh" -o "$tmp_update" 2>/dev/null; then
        if ! diff -q "$0" "$tmp_update" > /dev/null 2>&1; then
            spin_stop
            dim "Updater has a new version, restarting..."
            cp "$tmp_update" "$0"
            chmod +x "$0"
            rm -f "$tmp_update"
            export ODIN_SELF_UPDATED=1
            exec "$0" "$@"
        fi
    fi
    rm -f "$tmp_update"
    spin_stop
fi

# ── Phase 1: Current version ─────────────────────────────────────────────────

phase 1 $TOTAL "Checking current version"

CURRENT_VERSION=""
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^odin$'; then
    CURRENT_VERSION=$(docker exec odin cat /app/VERSION 2>/dev/null || echo "")
fi

if [ -z "$CURRENT_VERSION" ]; then
    CURRENT_VERSION=$(docker inspect "$ODIN_IMAGE" --format='{{index .Config.Labels "org.opencontainers.image.version"}}' 2>/dev/null || echo "unknown")
fi

ok "Current: ${CURRENT_VERSION}"

# ── Phase 2: Check for updates ────────────────────────────────────────────────

phase 2 $TOTAL "Checking for updates"

spin_start "Fetching latest version..."
LATEST_VERSION=""
# Check GitHub for the latest VERSION file
LATEST_VERSION=$(curl -sfL "${ODIN_REPO}/VERSION" 2>/dev/null || echo "")
spin_stop

if [ -z "$LATEST_VERSION" ]; then
    warn "Could not determine latest version, pulling anyway"
    LATEST_VERSION="unknown"
elif [ "$CURRENT_VERSION" = "$LATEST_VERSION" ] && [ "$FORCE" = false ]; then
    ok "Already up to date! (${CURRENT_VERSION})"
    printf "\n  ${DIM}Use --force to pull anyway.${RESET}\n\n"
    exit 0
fi

ok "Latest:  ${LATEST_VERSION}"

if [ "$CURRENT_VERSION" != "$LATEST_VERSION" ]; then
    ok "Update available: ${CURRENT_VERSION} → ${LATEST_VERSION}"
elif [ "$FORCE" = true ]; then
    ok "Force pulling (${CURRENT_VERSION})"
fi

# ── Phase 3: Pull image ──────────────────────────────────────────────────────

phase 3 $TOTAL "Pulling new image"

spin_start "Pulling ${ODIN_IMAGE}..."
if ! docker pull "$ODIN_IMAGE" > /dev/null 2>&1; then
    spin_stop
    die "Failed to pull ${ODIN_IMAGE}" \
        "Check your internet connection." \
        "Try manually: docker pull ${ODIN_IMAGE}"
fi
spin_stop

image_size=$(docker image inspect "$ODIN_IMAGE" --format='{{.Size}}' 2>/dev/null || echo "0")
image_mb=$(( image_size / 1048576 ))
ok "Pulled (${image_mb} MB)"

# ── Phase 4: Restart ─────────────────────────────────────────────────────────

phase 4 $TOTAL "Restarting O.D.I.N."

ENV_ARGS=""
[ -f .env ] && ENV_ARGS="--env-file .env"

if ! docker compose $ENV_ARGS up -d 2>/dev/null; then
    die "Failed to restart container" \
        "Check: docker compose logs" \
        "Rollback: docker pull ghcr.io/hughkantsime/odin:v${CURRENT_VERSION} && docker compose up -d"
fi
ok "Container restarted"

# ── Phase 5: Health check ────────────────────────────────────────────────────

phase 5 $TOTAL "Waiting for healthy"

attempts=0
max_attempts=60

spin_start "Waiting for health check..."
while [ $attempts -lt $max_attempts ]; do
    health=$(docker inspect --format='{{.State.Health.Status}}' odin 2>/dev/null || echo "starting")

    if [ "$health" = "healthy" ]; then
        spin_stop
        ok "O.D.I.N. is healthy"
        break
    fi

    attempts=$(( attempts + 1 ))
    sleep 1
done

if [ $attempts -ge $max_attempts ]; then
    spin_stop
    err "Container did not become healthy within ${max_attempts}s"
    dim "Check logs: docker compose logs"
    dim "Rollback:   docker pull ghcr.io/hughkantsime/odin:v${CURRENT_VERSION}"
    dim "            docker compose up -d"
    UPDATE_FAILED=1
    exit 1
fi

# ── Phase 6: Verify ──────────────────────────────────────────────────────────

phase 6 $TOTAL "Verifying update"

NEW_VERSION=$(docker exec odin cat /app/VERSION 2>/dev/null || echo "$LATEST_VERSION")
ok "Version: ${NEW_VERSION}"

# ── Success ───────────────────────────────────────────────────────────────────

ELAPSED=$(( $(date +%s) - START_TIME ))
HOST_IP=$(detect_ip)

draw_box "O.D.I.N. updated!" \
    "Version|${CURRENT_VERSION} → ${NEW_VERSION}" \
    "URL|http://${HOST_IP}:8000" \
    "Changelog|github.com/HughKantsime/runsodin/releases"

printf "\n  ${DIM}Updated in %s seconds.${RESET}\n\n" "$ELAPSED"
