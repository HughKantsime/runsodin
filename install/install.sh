#!/usr/bin/env bash
# O.D.I.N. Installer — curl -sfL https://raw.githubusercontent.com/HughKantsime/runsodin/master/install/install.sh | bash
set -euo pipefail

# Windows detection: if running under Cygwin, MSYS2, or Git Bash, redirect to PowerShell installer
case "$(uname -s 2>/dev/null)" in
    CYGWIN*|MINGW*|MSYS*)
        echo ""
        echo "  This script is for Linux and macOS."
        echo "  On Windows, use the PowerShell installer instead:"
        echo ""
        echo "    irm https://raw.githubusercontent.com/HughKantsime/runsodin/master/install/install.ps1 | iex"
        echo ""
        echo "  See: install/WINDOWS_INSTALL.md for full instructions."
        echo ""
        exit 1
        ;;
esac

ODIN_VERSION="1.3.70"
ODIN_IMAGE="ghcr.io/hughkantsime/odin:latest"
ODIN_REPO="https://raw.githubusercontent.com/HughKantsime/runsodin/master"
INSTALL_DIR="./odin"

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
info() { printf "  ${CYAN}▸${RESET} %s" "$*"; }

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
    # Usage: draw_box "Title" "key1|value1" "key2|value2" ...
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
    local exit_code=$?
    spin_stop
    if [ "${INSTALL_FAILED:-}" = "1" ]; then
        printf "\n${RED}Installation failed.${RESET}\n"
        dim "If the problem persists, open an issue at:"
        dim "https://github.com/HughKantsime/runsodin/issues"
    elif [ $exit_code -ne 0 ] && [ "${INSTALL_FAILED:-}" != "1" ]; then
        printf "\n${YELLOW}Interrupted.${RESET}\n"
    fi
}

trap cleanup EXIT
trap 'INSTALL_FAILED=0; exit 130' INT

die() {
    spin_stop
    err "$1"
    [ -n "${2:-}" ] && dim "$2"
    [ -n "${3:-}" ] && dim "$3"
    INSTALL_FAILED=1
    exit 1
}

prompt() {
    local msg="$1" default="$2" var="$3"
    local answer=""
    if [ "$IS_TTY" = true ]; then
        printf "  ${CYAN}▸${RESET} %s ${DIM}[%s]${RESET}: " "$msg" "$default"
        read -r answer </dev/tty || answer=""
        answer="${answer:-$default}"
    else
        answer="$default"
    fi
    printf -v "$var" '%s' "$answer"
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
    printf "    ${DIM}v%s — Orchestrated Dispatch & Inventory Network${RESET}\n" "$ODIN_VERSION"
    printf "    ${DIM}3D Print Farm Management${RESET}\n"
}

# ─── Utilities ─────────────────────────────────────────────────────────────────

detect_ip() {
    local ip=""
    # Try hostname -I first (Linux)
    if command -v hostname &>/dev/null; then
        ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    fi
    # Fallback: ip route
    if [ -z "$ip" ] && command -v ip &>/dev/null; then
        ip=$(ip route get 1.1.1.1 2>/dev/null | awk '/src/ {for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}')
    fi
    # Fallback: ifconfig
    if [ -z "$ip" ] && command -v ifconfig &>/dev/null; then
        ip=$(ifconfig 2>/dev/null | awk '/inet / && !/127.0.0.1/ {print $2; exit}')
    fi
    echo "${ip:-localhost}"
}

detect_timezone() {
    local tz=""
    # timedatectl
    if command -v timedatectl &>/dev/null; then
        tz=$(timedatectl show --property=Timezone --value 2>/dev/null || true)
    fi
    # /etc/timezone
    if [ -z "$tz" ] && [ -f /etc/timezone ]; then
        tz=$(cat /etc/timezone 2>/dev/null)
    fi
    # readlink /etc/localtime
    if [ -z "$tz" ]; then
        tz=$(readlink /etc/localtime 2>/dev/null | sed 's|.*/zoneinfo/||' || true)
    fi
    # TZ env var
    if [ -z "$tz" ]; then
        tz="${TZ:-}"
    fi
    echo "${tz:-America/New_York}"
}

check_port() {
    local port="$1"
    if command -v ss &>/dev/null; then
        ss -tlnp 2>/dev/null | grep -qE ":${port}\b" && return 1
    elif command -v lsof &>/dev/null; then
        lsof -iTCP:"$port" -sTCP:LISTEN &>/dev/null && return 1
    elif command -v netstat &>/dev/null; then
        netstat -tlnp 2>/dev/null | grep -qE ":${port}\b" && return 1
    else
        warn "Cannot check port ${port} — no ss, lsof, or netstat found"
        return 0
    fi
    return 0
}

port_user() {
    local port="$1"
    if command -v ss &>/dev/null; then
        ss -tlnp 2>/dev/null | grep ":${port} " | head -1
    elif command -v lsof &>/dev/null; then
        lsof -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | tail -1
    fi
}

# ─── Main ──────────────────────────────────────────────────────────────────────

START_TIME=$(date +%s)

banner

TOTAL=9

# ── Phase 1: Preflight ────────────────────────────────────────────────────────

phase 1 $TOTAL "Preflight checks"

# Docker
if ! command -v docker &>/dev/null; then
    err "Docker not found"
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        case "${ID:-}" in
            ubuntu|debian) dim "Install: curl -fsSL https://get.docker.com | sh" ;;
            fedora|centos|rhel) dim "Install: sudo dnf install docker-ce" ;;
            arch) dim "Install: sudo pacman -S docker" ;;
            *) dim "Install: https://docs.docker.com/engine/install/" ;;
        esac
    else
        dim "Install: https://docs.docker.com/engine/install/"
    fi
    INSTALL_FAILED=1; exit 1
fi

docker_version=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo "")
if [ -z "$docker_version" ]; then
    err "Docker daemon not running"
    dim "Start it: sudo systemctl start docker"
    dim "Then re-run this installer."
    INSTALL_FAILED=1; exit 1
fi
ok "Docker $docker_version"

# Docker Compose
if docker compose version &>/dev/null; then
    compose_version=$(docker compose version --short 2>/dev/null)
    ok "Docker Compose $compose_version"
elif command -v docker-compose &>/dev/null; then
    compose_version=$(docker-compose version --short 2>/dev/null)
    ok "Docker Compose $compose_version (standalone)"
else
    die "Docker Compose not found" \
        "Install: https://docs.docker.com/compose/install/"
fi

# curl
if ! command -v curl &>/dev/null; then
    die "curl not found" \
        "Install: sudo apt install curl (Debian/Ubuntu)" \
        "         sudo dnf install curl (Fedora/RHEL)"
fi
ok "curl available"

# Ports
port_fail=0
for port in 8000 1984 8555; do
    if ! check_port "$port"; then
        err "Port $port in use"
        dim "$(port_user "$port")"
        port_fail=1
    fi
done
if [ "$port_fail" = "1" ]; then
    die "Required ports are in use" \
        "Free the ports above and re-run the installer."
fi
ok "Ports 8000, 1984, 8555 free"

# Architecture
arch=$(uname -m)
case "$arch" in
    x86_64|amd64) ok "Architecture: amd64" ;;
    aarch64|arm64) warn "Architecture: arm64 (experimental — amd64 recommended)" ;;
    *) warn "Architecture: $arch (untested — amd64 recommended)" ;;
esac

# Disk space
if command -v df &>/dev/null; then
    free_kb=$(df -k . 2>/dev/null | awk 'NR==2 {print $4}')
    if [ -n "$free_kb" ]; then
        free_gb=$(( free_kb / 1048576 ))
        if [ "$free_gb" -lt 2 ]; then
            warn "Disk space: ${free_gb} GB free (2 GB minimum recommended)"
        else
            ok "Disk space: ${free_gb} GB free"
        fi
    fi
fi

# Existing install
if [ -f "${INSTALL_DIR}/docker-compose.yml" ]; then
    warn "Existing installation found at ${INSTALL_DIR}/"
    if [ "$IS_TTY" = true ]; then
        printf "  ${CYAN}▸${RESET} Reinstall? Existing data in odin-data/ will be preserved. ${DIM}[y/N]${RESET}: "
        read -r answer </dev/tty || answer=""
        case "$answer" in
            [yY]*) ok "Reinstalling..." ;;
            *)
                printf "\n  Use ${BOLD}cd odin && ./update.sh${RESET} to update instead.\n\n"
                exit 0
                ;;
        esac
    else
        dim "Existing install detected in non-interactive mode. Re-installing."
    fi
fi

# ── Phase 2: Configuration ────────────────────────────────────────────────────

phase 2 $TOTAL "Configuration"

DEFAULT_IP=$(detect_ip)
DEFAULT_TZ=$(detect_timezone)

prompt "Host IP for camera streaming" "$DEFAULT_IP" HOST_IP
prompt "Timezone" "$DEFAULT_TZ" TIMEZONE

ok "Host IP: $HOST_IP"
ok "Timezone: $TIMEZONE"

# ── Phase 3: Install directory ─────────────────────────────────────────────────

phase 3 $TOTAL "Creating install directory"

mkdir -p "${INSTALL_DIR}"
ok "Created ${INSTALL_DIR}/"

# ── Phase 4: Download configuration ───────────────────────────────────────────

phase 4 $TOTAL "Downloading configuration"

spin_start "Downloading docker-compose.yml..."
if ! curl -sfL "${ODIN_REPO}/install/docker-compose.yml" -o "${INSTALL_DIR}/docker-compose.yml"; then
    spin_stop
    die "Failed to download docker-compose.yml" \
        "Check your internet connection and try again." \
        "URL: ${ODIN_REPO}/install/docker-compose.yml"
fi
spin_stop
ok "docker-compose.yml"

spin_start "Downloading update.sh..."
if ! curl -sfL "${ODIN_REPO}/install/update.sh" -o "${INSTALL_DIR}/update.sh"; then
    spin_stop
    die "Failed to download update.sh" \
        "Check your internet connection and try again."
fi
chmod +x "${INSTALL_DIR}/update.sh"
spin_stop
ok "update.sh"

# ── Phase 5: Generate environment ─────────────────────────────────────────────

phase 5 $TOTAL "Generating environment"

cat > "${INSTALL_DIR}/.env" << EOF
# O.D.I.N. Environment — generated by installer
ODIN_HOST_IP=${HOST_IP}
TZ=${TIMEZONE}
CORS_ORIGINS=http://${HOST_IP}:8000,http://localhost:8000,http://localhost:3000
EOF

chmod 600 "${INSTALL_DIR}/.env"
ok ".env written"

# ── Phase 6: Pull image ───────────────────────────────────────────────────────

phase 6 $TOTAL "Pulling Docker image"

spin_start "Pulling ${ODIN_IMAGE}..."
if ! docker pull "$ODIN_IMAGE" > /dev/null 2>&1; then
    spin_stop
    die "Failed to pull ${ODIN_IMAGE}" \
        "Check your internet connection and Docker Hub access." \
        "Try manually: docker pull ${ODIN_IMAGE}"
fi
spin_stop

image_size=$(docker image inspect "$ODIN_IMAGE" --format='{{.Size}}' 2>/dev/null || echo "0")
image_mb=$(( image_size / 1048576 ))
ok "Pulled ${ODIN_IMAGE} (${image_mb} MB)"

# ── Phase 7: Start container ──────────────────────────────────────────────────

phase 7 $TOTAL "Starting O.D.I.N."

compose_output=$(docker compose -f "${INSTALL_DIR}/docker-compose.yml" --env-file "${INSTALL_DIR}/.env" up -d 2>&1) || {
    die "Failed to start container" \
        "$compose_output" \
        "Check: docker compose -f ${INSTALL_DIR}/docker-compose.yml logs"
}
ok "Container started"

# ── Phase 8: Health check ─────────────────────────────────────────────────────

phase 8 $TOTAL "Waiting for healthy"

messages=("Initializing database..." "Starting services..." "Loading configuration..." "Almost ready...")
msg_idx=0
attempts=0
max_attempts=60

spin_start "${messages[$msg_idx]}"
while [ $attempts -lt $max_attempts ]; do
    health=$(docker inspect --format='{{.State.Health.Status}}' odin 2>/dev/null || echo "starting")

    if [ "$health" = "healthy" ]; then
        spin_stop
        ok "O.D.I.N. is healthy"
        break
    fi

    # Rotate status messages
    new_idx=$(( attempts / 15 ))
    if [ $new_idx -ne $msg_idx ] && [ $new_idx -lt ${#messages[@]} ]; then
        msg_idx=$new_idx
        spin_stop
        spin_start "${messages[$msg_idx]}"
    fi

    attempts=$(( attempts + 1 ))
    sleep 1
done

if [ $attempts -ge $max_attempts ]; then
    spin_stop
    die "Container did not become healthy within ${max_attempts}s" \
        "Check logs: docker compose -f ${INSTALL_DIR}/docker-compose.yml logs" \
        "The container may still be starting — wait and check: docker ps"
fi

# Verify API
if curl -sf "http://localhost:8000/health" > /dev/null 2>&1; then
    ok "API responding on port 8000"
else
    warn "API not yet responding on localhost:8000 (may need a moment)"
fi

# ── Phase 9: Complete ─────────────────────────────────────────────────────────

phase 9 $TOTAL "Complete!"

ELAPSED=$(( $(date +%s) - START_TIME ))

draw_box "O.D.I.N. is ready!" \
    "URL|http://${HOST_IP}:8000" \
    "Setup|Create your admin account in the browser" \
    "Data|${INSTALL_DIR}/odin-data/" \
    "Logs|cd odin && docker compose logs -f" \
    "Update|cd odin && ./update.sh"

printf "\n  ${DIM}Installed in %s seconds.${RESET}\n\n" "$ELAPSED"
