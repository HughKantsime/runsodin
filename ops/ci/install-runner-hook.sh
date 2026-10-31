#!/usr/bin/env bash
# Install the job-started hook on odin-ci, as the runner account (see ops/ci/README.md).
# Root owns the installed copy, so a job can't rewrite it without going through Docker first.
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
sudo install -d -m 0755 -o root -g root "$(dirname "$hook")"
sudo install -m 0755 -o root -g root "$here/runner-job-started-hook.sh" "$hook"
if runner_env_set ACTIONS_RUNNER_HOOK_JOB_STARTED "$hook"; then
  runner_restart
fi
echo "hook installed at $hook; a job not dispatched by the owner now fails at 'Set up runner'"
