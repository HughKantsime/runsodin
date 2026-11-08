#!/usr/bin/env bash
# Run from a candidate checkout on odin-ci; no Actions session needed.
set -euo pipefail
umask 022
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo"
test "$(uname -s)" = Linux || { echo "Run this precheck on odin-ci (Linux)" >&2; exit 1; }
# SSH shells must not inject make flags or alternate Python module resolution.
unset MAKEFLAGS MFLAGS MAKEOVERRIDES PYTHONPATH PYTHONHOME
python3.11 -c 'import sys; sys.version_info[:2] == (3, 11) or sys.exit("Python 3.11 is required")'
python="$repo/.venv-contracts/bin/python"
export PATH="$repo/.venv-contracts/bin:$PATH"
export DOCKER_HOST=unix:///var/run/docker.sock
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-${XDG_CACHE_HOME:-$HOME/.cache}/ms-playwright}"
export CI=true
export RELEASE_CONTROL_RUN_ID="${RELEASE_CONTROL_RUN_ID:-vm-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
[[ "$RELEASE_CONTROL_RUN_ID" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ ]] || { echo "Invalid precheck run ID" >&2; exit 1; }
# Protected JSON checks every ancestor. /tmp (1777) cannot contain these homes.
# Refuse unsafe existing ancestors; create only private directories we own.
private_tmp="$(python3.11 - <<'PY'
import os
from pathlib import Path
import stat
home = Path.home().resolve()
for parent in (home, *home.parents):
    if parent.stat().st_mode & 0o022:
        raise SystemExit(f"Precheck home ancestor is group/world writable: {parent}")
parent = home
for part in ('.cache', 'odin-precheck', os.environ['RELEASE_CONTROL_RUN_ID']):
    parent = parent / part
    parent.mkdir(mode=0o700, exist_ok=True)
    info = parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise SystemExit(f"Unsafe precheck directory: {parent}")
# Never reuse or remove temporary data from another run.
root = parent / 'tmp'
root.mkdir(mode=0o700)
print(root)
PY
)"
cleanup() { rm -rf -- "${private_tmp:?}"; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
export TMPDIR="$private_tmp" TMP="$private_tmp" TEMP="$private_tmp"
printf 'VM precheck %s; umask %s; all ten components\n' "$RELEASE_CONTROL_RUN_ID" "$(umask)"
# Bootstrap is also serialized: it must not mutate a runner's active environments.
python3.11 -m ops.release_control.host_lock --precheck bash -c '
  set -euo pipefail
  bash ops/release_control/bootstrap_validation_deps.sh
  exec make trusted-validation-gate PYTHON="$1"
' precheck "$python"
