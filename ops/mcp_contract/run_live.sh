#!/bin/bash
# Usage: ops/mcp_contract/run_live.sh PACKAGE_DIR
# Requires installed Python 3.11/backend dependencies and package node_modules.
# Local disposable SQLite + Moonraker only; no Docker, installs, or real printers.
# Exit: npm suite's exit code; 2 usage/setup/refused configuration; 130/143 signal.
# Always stop the owned process and delete its private directory, even on failure.
set -eu
set +x
[ "$#" -eq 1 ] || { echo "Usage: $0 PACKAGE_DIR" >&2; exit 2; }
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
package_dir=$(CDPATH= cd -- "$1" && pwd) || exit 2
run_dir=$(mktemp -d "${TMPDIR:-/tmp}/odin-mcp-live.XXXXXX")
runner_pid=
cleanup() {
    if [ -n "$runner_pid" ]; then
        kill -TERM "$runner_pid" 2>/dev/null || true
        wait "$runner_pid" 2>/dev/null || true
    fi
    rm -rf -- "${run_dir:?}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
python3.11 "$script_dir/run_live.py" "$package_dir" "$run_dir" &
runner_pid=$!
set +e
wait "$runner_pid"
result=$?
runner_pid=
exit "$result"
