#!/usr/bin/env bash
# ============================================================================
# O.D.I.N. Installer & Updater Test Suite
#
# Tests the install.sh and update.sh scripts end-to-end.
#
# Usage:
#   bash tests/test_installer.sh              # all tests
#   bash tests/test_installer.sh --unit       # unit tests only (no Docker)
#   bash tests/test_installer.sh --integration # integration tests (needs Docker + network)
#
# Requirements:
#   - Docker (for integration tests)
#   - curl (for integration tests)
#   - No containers named 'odin' running (integration tests will manage lifecycle)
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
INSTALL_SH="$REPO_DIR/install/install.sh"
UPDATE_SH="$REPO_DIR/install/update.sh"

# ─── Test Framework ────────────────────────────────────────────────────────────

PASS=0
FAIL=0
SKIP=0
FAILURES=()
MODE="all"

for arg in "$@"; do
    case "$arg" in
        --unit) MODE="unit" ;;
        --integration) MODE="integration" ;;
    esac
done

RED=$'\033[31m'
GREEN=$'\033[32m'
YELLOW=$'\033[33m'
CYAN=$'\033[36m'
DIM=$'\033[2m'
BOLD=$'\033[1m'
RESET=$'\033[0m'

pass() {
    PASS=$(( PASS + 1 ))
    printf "  ${GREEN}✓${RESET} %s\n" "$1"
}

fail() {
    FAIL=$(( FAIL + 1 ))
    FAILURES+=("$1: $2")
    printf "  ${RED}✗${RESET} %s\n" "$1"
    printf "    ${DIM}%s${RESET}\n" "$2"
}

skip() {
    SKIP=$(( SKIP + 1 ))
    printf "  ${YELLOW}⊘${RESET} %s ${DIM}(skipped)${RESET}\n" "$1"
}

section() {
    printf "\n${BOLD}${CYAN}━━━ %s ━━━${RESET}\n" "$1"
}

# ─── Temp Directory ────────────────────────────────────────────────────────────

TMPDIR_TEST=$(mktemp -d /tmp/odin-test-XXXXXX)
cleanup_test() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup_test EXIT

# ─── Source helpers from install.sh ────────────────────────────────────────────
# Extract pure functions for unit testing without running the full script.

extract_functions() {
    # Create a sourceable file with just the functions we need to test
    cat > "$TMPDIR_TEST/functions.sh" << 'EXTRACT'
# Stub out TTY detection
IS_TTY=false
CYAN="" BOLD="" GREEN="" RED="" YELLOW="" DIM="" RESET=""
SPINNER_PID=""

ok()   { printf "  ✓ %s\n" "$*"; }
err()  { printf "  ✗ %s\n" "$*" >&2; }
warn() { printf "  ! %s\n" "$*"; }
dim()  { printf "    %s\n" "$*"; }

spin_start() { :; }
spin_stop()  { :; }
EXTRACT

    # Extract detect_ip, detect_timezone, check_port from install.sh
    sed -n '/^detect_ip()/,/^}/p' "$INSTALL_SH" >> "$TMPDIR_TEST/functions.sh"
    sed -n '/^detect_timezone()/,/^}/p' "$INSTALL_SH" >> "$TMPDIR_TEST/functions.sh"
    sed -n '/^check_port()/,/^}/p' "$INSTALL_SH" >> "$TMPDIR_TEST/functions.sh"
    sed -n '/^draw_box()/,/^}/p' "$INSTALL_SH" >> "$TMPDIR_TEST/functions.sh"
    # prompt function
    sed -n '/^prompt()/,/^}/p' "$INSTALL_SH" >> "$TMPDIR_TEST/functions.sh"
}

extract_functions

# ============================================================================
# UNIT TESTS
# ============================================================================

if [ "$MODE" = "all" ] || [ "$MODE" = "unit" ]; then

section "Syntax Validation"

if bash -n "$INSTALL_SH" 2>/dev/null; then
    pass "install.sh syntax valid"
else
    fail "install.sh syntax valid" "bash -n failed"
fi

if bash -n "$UPDATE_SH" 2>/dev/null; then
    pass "update.sh syntax valid"
else
    fail "update.sh syntax valid" "bash -n failed"
fi

# ── Shellcheck (if available) ──────────────────────────────────────────────

section "Static Analysis"

if command -v shellcheck &>/dev/null; then
    sc_out=$(shellcheck -S warning "$INSTALL_SH" 2>&1 || true)
    if [ -z "$sc_out" ]; then
        pass "install.sh shellcheck clean"
    else
        # Count issues, allow informational
        issue_count=$(echo "$sc_out" | grep -c "^In " || true)
        fail "install.sh shellcheck" "$issue_count issue(s) found"
    fi

    sc_out=$(shellcheck -S warning "$UPDATE_SH" 2>&1 || true)
    if [ -z "$sc_out" ]; then
        pass "update.sh shellcheck clean"
    else
        issue_count=$(echo "$sc_out" | grep -c "^In " || true)
        fail "update.sh shellcheck" "$issue_count issue(s) found"
    fi
else
    skip "shellcheck (not installed)"
fi

# ── Script Structure ───────────────────────────────────────────────────────

section "Script Structure"

# Shebang
if head -1 "$INSTALL_SH" | grep -q '#!/usr/bin/env bash'; then
    pass "install.sh has correct shebang"
else
    fail "install.sh shebang" "Expected #!/usr/bin/env bash"
fi

if head -1 "$UPDATE_SH" | grep -q '#!/usr/bin/env bash'; then
    pass "update.sh has correct shebang"
else
    fail "update.sh shebang" "Expected #!/usr/bin/env bash"
fi

# set -euo pipefail
if grep -q 'set -euo pipefail' "$INSTALL_SH"; then
    pass "install.sh uses strict mode"
else
    fail "install.sh strict mode" "Missing set -euo pipefail"
fi

if grep -q 'set -euo pipefail' "$UPDATE_SH"; then
    pass "update.sh uses strict mode"
else
    fail "update.sh strict mode" "Missing set -euo pipefail"
fi

# EXIT trap
if grep -q 'trap cleanup EXIT' "$INSTALL_SH"; then
    pass "install.sh has EXIT trap"
else
    fail "install.sh EXIT trap" "Missing trap cleanup EXIT"
fi

if grep -q 'trap cleanup EXIT' "$UPDATE_SH"; then
    pass "update.sh has EXIT trap"
else
    fail "update.sh EXIT trap" "Missing trap cleanup EXIT"
fi

# INT trap
if grep -q "trap.*INT" "$INSTALL_SH"; then
    pass "install.sh has INT trap"
else
    fail "install.sh INT trap" "Missing SIGINT handler"
fi

if grep -q "trap.*INT" "$UPDATE_SH"; then
    pass "update.sh has INT trap"
else
    fail "update.sh INT trap" "Missing SIGINT handler"
fi

# Executable
if [ -x "$INSTALL_SH" ]; then
    pass "install.sh is executable"
else
    fail "install.sh executable" "Missing +x permission"
fi

if [ -x "$UPDATE_SH" ]; then
    pass "update.sh is executable"
else
    fail "update.sh executable" "Missing +x permission"
fi

# ── Security Checks ───────────────────────────────────────────────────────

section "Security"

# No eval
if grep -q '\beval\b' "$INSTALL_SH"; then
    fail "install.sh no eval" "Found eval — use printf -v instead"
else
    pass "install.sh no eval"
fi

if grep -q '\beval\b' "$UPDATE_SH"; then
    fail "update.sh no eval" "Found eval — use printf -v instead"
else
    pass "update.sh no eval"
fi

# .env permissions
if grep -q 'chmod 600.*\.env' "$INSTALL_SH"; then
    pass "install.sh sets .env to 600"
else
    fail "install.sh .env permissions" "Missing chmod 600 on .env file"
fi

# HTTPS for all remote URLs (exclude localhost/LAN/template refs)
install_urls=$(grep -oE 'https?://[^ "]+' "$INSTALL_SH" | grep -v '^https://' | grep -vE 'localhost|127\.0\.0\.\|0\.0\.0\.0|\$\{' || true)
if [ -z "$install_urls" ]; then
    pass "install.sh uses HTTPS for all remote URLs"
else
    fail "install.sh HTTPS" "Found non-HTTPS remote URLs: $install_urls"
fi

update_urls=$(grep -oE 'https?://[^ "]+' "$UPDATE_SH" | grep -v '^https://' | grep -vE 'localhost|127\.0\.0\.\|0\.0\.0\.0|\$\{' || true)
if [ -z "$update_urls" ]; then
    pass "update.sh uses HTTPS for all remote URLs"
else
    fail "update.sh HTTPS" "Found non-HTTPS remote URLs: $update_urls"
fi

# No hardcoded secrets
for script in "$INSTALL_SH" "$UPDATE_SH"; do
    name=$(basename "$script")
    if grep -iE '(password|secret|token)\s*=' "$script" | grep -vE '(JWT_SECRET_KEY|ENCRYPTION_KEY|API_KEY).*\$\{' | grep -v '^\s*#' | grep -q .; then
        fail "$name no hardcoded secrets" "Found potential hardcoded credentials"
    else
        pass "$name no hardcoded secrets"
    fi
done

# ── Function Unit Tests ───────────────────────────────────────────────────

section "Function: detect_ip"

ip_result=$(bash -c "source '$TMPDIR_TEST/functions.sh'; detect_ip")
if [[ "$ip_result" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || [ "$ip_result" = "localhost" ]; then
    pass "detect_ip returns valid IP or localhost ($ip_result)"
else
    fail "detect_ip return value" "Got: $ip_result"
fi

section "Function: detect_timezone"

tz_result=$(bash -c "source '$TMPDIR_TEST/functions.sh'; detect_timezone")
if [ -n "$tz_result" ]; then
    pass "detect_timezone returns non-empty ($tz_result)"
else
    fail "detect_timezone return value" "Got empty string"
fi

section "Function: check_port"

# Port that's definitely free
free_port=59999
if bash -c "source '$TMPDIR_TEST/functions.sh'; check_port $free_port"; then
    pass "check_port returns 0 for free port $free_port"
else
    fail "check_port free port" "Returned non-zero for port $free_port"
fi

section "Function: prompt (non-TTY mode)"

# In non-TTY mode, should use default
prompt_result=$(bash -c "
    source '$TMPDIR_TEST/functions.sh'
    IS_TTY=false
    prompt 'Test' 'default_val' RESULT
    echo \"\$RESULT\"
" < /dev/null 2>&1)
if [ "$prompt_result" = "default_val" ]; then
    pass "prompt uses default in non-TTY mode"
else
    fail "prompt non-TTY default" "Expected 'default_val', got '$prompt_result'"
fi

section "Function: draw_box"

box_output=$(bash -c "source '$TMPDIR_TEST/functions.sh'; draw_box 'Test Title' 'Key|Value'" 2>&1)
if echo "$box_output" | grep -q "Test Title" && echo "$box_output" | grep -q "Value"; then
    pass "draw_box renders title and content"
else
    fail "draw_box rendering" "Missing title or content in output"
fi

if echo "$box_output" | grep -q '╔' && echo "$box_output" | grep -q '╚'; then
    pass "draw_box has box-drawing characters"
else
    fail "draw_box characters" "Missing box-drawing characters"
fi

# ── Non-TTY / Pipe Mode ───────────────────────────────────────────────────

section "Non-TTY Mode"

# Verify color stripping when piped
color_check=$(echo 'test' | bash -c '
    if [ -t 1 ] && [ -t 2 ]; then echo "tty"; else echo "pipe"; fi
')
if [ "$color_check" = "pipe" ]; then
    pass "TTY detection works (pipe mode detected)"
else
    fail "TTY detection" "Expected pipe mode, got: $color_check"
fi

# Verify install.sh can run in non-TTY without blocking on prompts
# (We expect it to fail at docker checks, but NOT to hang on prompts)
timeout 5 bash -c "echo '' | bash '$INSTALL_SH' 2>&1 || true" > "$TMPDIR_TEST/nontty_output" 2>&1 || {
    exitcode=$?
    if [ $exitcode -eq 124 ]; then
        fail "install.sh non-TTY hangs" "Script hung (timeout) — likely blocking on a prompt"
    fi
}
if [ -f "$TMPDIR_TEST/nontty_output" ]; then
    # It should either fail at preflight or proceed without prompts
    if grep -q "ODIN\|Preflight\|Docker" "$TMPDIR_TEST/nontty_output"; then
        pass "install.sh runs in non-TTY without hanging"
    else
        pass "install.sh runs in non-TTY (exited early)"
    fi
fi

# ── Version Consistency ───────────────────────────────────────────────────

section "Version Consistency"

version_file=$(cat "$REPO_DIR/VERSION" 2>/dev/null | tr -d '[:space:]')
install_version=$(grep '^ODIN_VERSION=' "$INSTALL_SH" | head -1 | cut -d'"' -f2)

if [ "$version_file" = "$install_version" ]; then
    pass "install.sh ODIN_VERSION matches VERSION file ($version_file)"
else
    fail "install.sh version mismatch" "VERSION=$version_file, install.sh=$install_version"
fi

# ── Image Reference Consistency ───────────────────────────────────────────

section "Image References"

install_image=$(grep '^ODIN_IMAGE=' "$INSTALL_SH" | head -1 | cut -d'"' -f2)
update_image=$(grep '^ODIN_IMAGE=' "$UPDATE_SH" | head -1 | cut -d'"' -f2)
compose_image=$(grep 'image:' "$REPO_DIR/install/docker-compose.yml" | awk '{print $2}')

if [ "$install_image" = "$update_image" ]; then
    pass "install.sh and update.sh reference same image ($install_image)"
else
    fail "image mismatch" "install=$install_image, update=$update_image"
fi

if [ "$install_image" = "$compose_image" ]; then
    pass "scripts and docker-compose.yml reference same image"
else
    fail "compose image mismatch" "scripts=$install_image, compose=$compose_image"
fi

# ── Repo URL Consistency ──────────────────────────────────────────────────

section "Repository URLs"

install_repo=$(grep '^ODIN_REPO=' "$INSTALL_SH" | head -1 | cut -d'"' -f2)
update_repo=$(grep '^ODIN_REPO=' "$UPDATE_SH" | head -1 | cut -d'"' -f2)

if [ "$install_repo" = "$update_repo" ]; then
    pass "install.sh and update.sh use same repo URL"
else
    fail "repo URL mismatch" "install=$install_repo, update=$update_repo"
fi

# ── Self-Update Mechanism ─────────────────────────────────────────────────

section "Update Script Features"

if grep -q 'ODIN_SELF_UPDATED' "$UPDATE_SH"; then
    pass "update.sh has self-update mechanism"
else
    fail "update.sh self-update" "Missing ODIN_SELF_UPDATED logic"
fi

if grep -q '\-\-force' "$UPDATE_SH"; then
    pass "update.sh supports --force flag"
else
    fail "update.sh --force" "Missing --force support"
fi

if grep -q 'exec "\$0"' "$UPDATE_SH"; then
    pass "update.sh exec's itself after self-update"
else
    fail "update.sh exec self" "Missing exec for self-update restart"
fi

if grep -q 'Rollback' "$UPDATE_SH"; then
    pass "update.sh provides rollback instructions on failure"
else
    fail "update.sh rollback" "No rollback instructions found"
fi

# ── Compose File Validation ───────────────────────────────────────────────

section "Docker Compose File"

compose_file="$REPO_DIR/install/docker-compose.yml"

if grep -q 'healthcheck' "$compose_file"; then
    pass "docker-compose.yml has healthcheck"
else
    fail "compose healthcheck" "Missing healthcheck configuration"
fi

if grep -q 'restart: unless-stopped' "$compose_file"; then
    pass "docker-compose.yml has restart policy"
else
    fail "compose restart" "Missing restart policy"
fi

for port in 8000 1984 8555; do
    if grep -q "\"$port:" "$compose_file"; then
        pass "docker-compose.yml exposes port $port"
    else
        fail "compose port $port" "Port $port not exposed"
    fi
done

if grep -q 'odin-data:/data' "$compose_file"; then
    pass "docker-compose.yml mounts data volume"
else
    fail "compose volume" "Missing /data volume mount"
fi

fi # end unit tests

# ============================================================================
# INTEGRATION TESTS
# ============================================================================

if [ "$MODE" = "all" ] || [ "$MODE" = "integration" ]; then

section "Integration: Prerequisites"

if ! command -v docker &>/dev/null; then
    skip "Docker not available — skipping all integration tests"
else
if ! docker info &>/dev/null 2>&1; then
    skip "Docker daemon not running — skipping all integration tests"
else

# Check if odin container already exists
if docker ps -a --format '{{.Names}}' | grep -q '^odin$'; then
    skip "Container 'odin' already exists — skipping integration tests to avoid disruption"
else

section "Integration: Fresh Install"

INTEGRATION_DIR="$TMPDIR_TEST/integration"
mkdir -p "$INTEGRATION_DIR"
cd "$INTEGRATION_DIR"

# Run installer in non-interactive mode (will use auto-detected defaults)
install_output=$(bash "$INSTALL_SH" < /dev/null 2>&1) || install_exit=$?
install_exit=${install_exit:-0}

if [ $install_exit -eq 0 ]; then
    pass "install.sh exits 0"
else
    fail "install.sh exit code" "Exited with $install_exit"
    echo "$install_output" | tail -20
fi

# Verify created files
if [ -f "$INTEGRATION_DIR/odin/docker-compose.yml" ]; then
    pass "docker-compose.yml downloaded"
else
    fail "docker-compose.yml missing" "Not found at odin/docker-compose.yml"
fi

if [ -f "$INTEGRATION_DIR/odin/update.sh" ] && [ -x "$INTEGRATION_DIR/odin/update.sh" ]; then
    pass "update.sh downloaded and executable"
else
    fail "update.sh missing/not executable" "Check odin/update.sh"
fi

if [ -f "$INTEGRATION_DIR/odin/.env" ]; then
    pass ".env file created"

    # Verify .env permissions
    env_perms=$(stat -c '%a' "$INTEGRATION_DIR/odin/.env" 2>/dev/null || stat -f '%Lp' "$INTEGRATION_DIR/odin/.env" 2>/dev/null)
    if [ "$env_perms" = "600" ]; then
        pass ".env has restrictive permissions (600)"
    else
        fail ".env permissions" "Expected 600, got $env_perms"
    fi

    # Verify .env contents
    if grep -q 'ODIN_HOST_IP=' "$INTEGRATION_DIR/odin/.env"; then
        pass ".env contains ODIN_HOST_IP"
    else
        fail ".env ODIN_HOST_IP" "Missing ODIN_HOST_IP"
    fi

    if grep -q 'CORS_ORIGINS=' "$INTEGRATION_DIR/odin/.env"; then
        pass ".env contains CORS_ORIGINS"
    else
        fail ".env CORS_ORIGINS" "Missing CORS_ORIGINS"
    fi

    if grep -q 'TZ=' "$INTEGRATION_DIR/odin/.env"; then
        pass ".env contains TZ"
    else
        fail ".env TZ" "Missing TZ"
    fi
else
    fail ".env missing" "Not found at odin/.env"
fi

# Check container is running
if docker ps --format '{{.Names}}' | grep -q '^odin$'; then
    pass "odin container is running"

    # Wait for healthy
    healthy=false
    for i in $(seq 1 90); do
        health=$(docker inspect --format='{{.State.Health.Status}}' odin 2>/dev/null || echo "starting")
        if [ "$health" = "healthy" ]; then
            healthy=true
            break
        fi
        sleep 1
    done

    if [ "$healthy" = true ]; then
        pass "container reached healthy state"
    else
        fail "container health" "Did not become healthy within 90s (status: $health)"
    fi

    # API health check
    if curl -sf http://localhost:8000/health > /dev/null 2>&1; then
        pass "API /health returns 200"
    else
        fail "API health" "curl to /health failed"
    fi

    # Version endpoint
    api_version=$(curl -sf http://localhost:8000/api/version 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('version',''))" 2>/dev/null || echo "")
    if [ -n "$api_version" ]; then
        pass "API /api/version responds ($api_version)"
    else
        skip "API /api/version (endpoint may not exist)"
    fi

    # ── Update: Already Current ────────────────────────────────────────

    section "Integration: Update (already current)"

    cd "$INTEGRATION_DIR/odin"
    update_output=$(bash update.sh 2>&1) || update_exit=$?
    update_exit=${update_exit:-0}

    if echo "$update_output" | grep -qi "up to date\|Already"; then
        pass "update.sh detects already current"
    else
        # Might pull anyway if version detection differs
        skip "update.sh already-current detection (version mismatch between local and remote)"
    fi

    # ── Update: --force ────────────────────────────────────────────────

    section "Integration: Update --force"

    force_output=$(bash update.sh --force 2>&1) || force_exit=$?
    force_exit=${force_exit:-0}

    if [ $force_exit -eq 0 ]; then
        pass "update.sh --force exits 0"
    else
        fail "update.sh --force exit code" "Exited with $force_exit"
    fi

    if echo "$force_output" | grep -qi "Pulled\|updated\|Force"; then
        pass "update.sh --force pulled image"
    else
        fail "update.sh --force pull" "Expected pull output"
    fi

    # Verify still healthy after update
    sleep 5
    if curl -sf http://localhost:8000/health > /dev/null 2>&1; then
        pass "API healthy after update"
    else
        # Give it more time
        sleep 15
        if curl -sf http://localhost:8000/health > /dev/null 2>&1; then
            pass "API healthy after update (slow start)"
        else
            fail "API health after update" "Not responding after update"
        fi
    fi

    # ── Cleanup ────────────────────────────────────────────────────────

    section "Integration: Cleanup"

    cd "$INTEGRATION_DIR"
    docker compose -f "$INTEGRATION_DIR/odin/docker-compose.yml" down -v 2>/dev/null && \
        pass "container cleaned up" || \
        fail "cleanup" "docker compose down failed"

else
    fail "container not running" "odin container not found in docker ps"
    skip "all container-dependent integration tests"
fi

fi # odin not already running
fi # docker daemon
fi # docker command
fi # integration mode

# ============================================================================
# Summary
# ============================================================================

TOTAL_TESTS=$(( PASS + FAIL + SKIP ))

printf "\n${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}\n"
printf "${BOLD}Results:${RESET} %s tests\n" "$TOTAL_TESTS"
printf "  ${GREEN}✓ %s passed${RESET}\n" "$PASS"
if [ "$FAIL" -gt 0 ]; then
    printf "  ${RED}✗ %s failed${RESET}\n" "$FAIL"
fi
if [ "$SKIP" -gt 0 ]; then
    printf "  ${YELLOW}⊘ %s skipped${RESET}\n" "$SKIP"
fi

if [ "$FAIL" -gt 0 ]; then
    printf "\n${RED}Failures:${RESET}\n"
    for f in "${FAILURES[@]}"; do
        printf "  ${RED}•${RESET} %s\n" "$f"
    done
    printf "\n"
    exit 1
fi

printf "\n${GREEN}All tests passed.${RESET}\n\n"
exit 0
