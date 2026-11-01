#!/usr/bin/env bash
# Install the job-started hook, on odin-ci or the Mac, as the runner's account (see ops/ci/README.md).
# Root owns the installed copy, so a job can't rewrite it without root. Numeric owner and group
# because macOS has no group called root. Bash 3.2 compatible.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=ops/ci/runner-lib.sh
. "$here/runner-lib.sh"
hook=/usr/local/libexec/odin-ci/runner-job-started-hook.sh

if runner_job_active; then
  echo "a runner job is active; install the hook after it finishes" >&2
  exit 75
fi
command -v jq >/dev/null || { echo "jq is required by the hook" >&2; exit 1; }
sudo install -d -m 0755 -o 0 -g 0 "$(dirname "$hook")"
sudo install -m 0755 -o 0 -g 0 "$here/runner-job-started-hook.sh" "$hook"
if runner_env_set ACTIONS_RUNNER_HOOK_JOB_STARTED "$hook"; then
  runner_restart
fi
echo "hook installed at $hook; a job not dispatched by the owner now dies in 'Set up runner'"
