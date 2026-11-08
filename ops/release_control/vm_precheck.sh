#!/usr/bin/env bash
# Run from a prepared candidate checkout on odin-ci; no Actions session needed.
set -euo pipefail
umask 022
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo"
test "$(uname -s)" = Linux || { echo "Run this precheck on odin-ci (Linux)" >&2; exit 1; }
python="$repo/.venv-contracts/bin/python"
test -x "$python" || { echo "Bootstrap .venv-contracts as in trusted-validation.yml first" >&2; exit 1; }
"$python" -c 'import sys; sys.version_info[:2] == (3, 11) or sys.exit("Python 3.11 is required")'
export PATH="$repo/.venv-contracts/bin:$PATH"
export DOCKER_HOST=unix:///var/run/docker.sock
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-${XDG_CACHE_HOME:-$HOME/.cache}/ms-playwright}"
export CI=true
export RELEASE_CONTROL_RUN_ID="${RELEASE_CONTROL_RUN_ID:-vm-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
# SSH shells must not inject make flags or alternate Python module resolution.
unset MAKEFLAGS MFLAGS MAKEOVERRIDES PYTHONPATH PYTHONHOME
printf 'VM precheck %s; umask %s; all ten components\n' "$RELEASE_CONTROL_RUN_ID" "$(umask)"
exec make trusted-validation-gate PYTHON="$python"
