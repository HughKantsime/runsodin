#!/usr/bin/env bash
# Run an ad-hoc command on odin-ci: refuse while the runner has a job, and stop if one starts.
# A test run during a job skews the job's latency limits and can collide with its ports.
# Usage: vm-guarded-run.sh <command...>   Exit 75 means refused or stopped for a job.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=ops/ci/runner-lib.sh
. "$here/runner-lib.sh"
[ "$#" -gt 0 ] || { echo "usage: vm-guarded-run.sh <command...>" >&2; exit 2; }
if runner_job_active; then
  echo "refused: a runner job is active" >&2
  exit 75
fi
interval="${VM_GUARD_INTERVAL:-5}"
stopped="$(mktemp)"
# Its own process group, so stopping it reaches everything it started.
set -m
"$@" </dev/null &
pid=$!
set +m
stop_command() {
  local waited=0
  kill -TERM -- "-$pid" 2>/dev/null || return 0
  while [ "$waited" -lt "${VM_GUARD_GRACE:-10}" ] && kill -0 -- "-$pid" 2>/dev/null; do
    sleep 1
    waited=$((waited + 1))
  done
  kill -KILL -- "-$pid" 2>/dev/null || true
}
trap 'stop_command; rm -f "$stopped"' HUP INT TERM
(
  while kill -0 "$pid" 2>/dev/null; do
    sleep "$interval" >/dev/null 2>&1
    if runner_job_active; then
      echo "stopping: a runner job started" >&2
      echo stopped > "$stopped"
      stop_command
      exit 0
    fi
  done
) &
watcher=$!
rc=0
wait "$pid" || rc=$?
kill "$watcher" 2>/dev/null || true
wait "$watcher" 2>/dev/null || true
if [ -s "$stopped" ]; then rc=75; fi
rm -f "$stopped"
exit "$rc"
