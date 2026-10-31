#!/usr/bin/env bash
# Job-started hook for this repository's runners on odin-ci and the Mac, set through
# ACTIONS_RUNNER_HOOK_JOB_STARTED in the runner's .env. A job can read a runner's credentials
# and, on the Mac, the account's gh login and production SSH key, so a runner only runs what
# the owner dispatched on this repository.
#
# A failing hook doesn't stop a job. The runner treats it as an ordinary step, and later steps
# with `if: always()` still run. So anything short of the pass at the end of this script kills
# Runner.Worker, this script's parent, which ends the job at once. Only the direct parent can
# be killed: the tests run this script under real runners, as a descendant of their workers.
# The kill also stops the runner uploading this step's log, so each refusal is first appended
# to ~/.odin-runner-job-hook.log on this host. Bash 3.2 compatible.
set -euo pipefail
OWNER=HughKantsime
OWNER_ID=201174638
REPOSITORY=HughKantsime/runsodin
REPOSITORY_ID=1143865575
passed=0
why="the hook stopped before its checks finished"

# Best effort: a log that can't be written must never save a job from the kill.
record() {
  {
    printf '%s run=%s event=%s actor=%s repository=%s why=[%s] action=[%s]\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)" "${GITHUB_RUN_ID:-unset}" "${GITHUB_EVENT_NAME:-unset}" \
      "${GITHUB_ACTOR:-unset}" "${GITHUB_REPOSITORY:-unset}" "$why" "$1" >> "${HOME:-/nonexistent}/.odin-runner-job-hook.log"
  } 2>/dev/null || true
}

stop_job() {
  local parent
  [ "$passed" = 0 ] || return 0
  parent=$(ps -o comm= -p "$PPID" 2>/dev/null) || parent=""
  parent=${parent%% *}
  case "$parent" in
    Runner.Worker|*/Runner.Worker)
      record "killing Runner.Worker $PPID"
      echo "runner job hook: killing Runner.Worker $PPID so no later step runs" >&2
      kill -KILL "$PPID"
      ;;
    *)
      record "parent ${parent:-unknown} kept running"
      echo "runner job hook: parent ${parent:-unknown} is not Runner.Worker, so it keeps running" >&2
      ;;
  esac
}
trap stop_job EXIT

refuse() { why="$*"; echo "runner job hook refuses this job: $*" >&2; exit 1; }

[ "${GITHUB_EVENT_NAME:-}" = workflow_dispatch ] || refuse "event ${GITHUB_EVENT_NAME:-unset} is not workflow_dispatch"
[ "${GITHUB_REPOSITORY:-}" = "$REPOSITORY" ] || refuse "repository ${GITHUB_REPOSITORY:-unset} is not $REPOSITORY"
[ "${GITHUB_REPOSITORY_ID:-}" = "$REPOSITORY_ID" ] || refuse "repository id ${GITHUB_REPOSITORY_ID:-unset} is not $REPOSITORY_ID"
[ "${GITHUB_ACTOR:-}" = "$OWNER" ] || refuse "actor ${GITHUB_ACTOR:-unset} is not the owner"
[ "${GITHUB_ACTOR_ID:-}" = "$OWNER_ID" ] || refuse "actor id ${GITHUB_ACTOR_ID:-unset} is not the owner's"
[ "${GITHUB_TRIGGERING_ACTOR:-}" = "$OWNER" ] || refuse "triggering actor ${GITHUB_TRIGGERING_ACTOR:-unset} is not the owner"

# The runner writes the event payload before this hook. It has to tell the same story.
[ -f "${GITHUB_EVENT_PATH:-}" ] || refuse "no event payload"
jq -e --arg owner "$OWNER" --argjson owner_id "$OWNER_ID" --arg repository "$REPOSITORY" --argjson repository_id "$REPOSITORY_ID" '
  .sender.login == $owner and .sender.id == $owner_id
  and .repository.full_name == $repository and .repository.id == $repository_id
  and .pull_request == null
' "$GITHUB_EVENT_PATH" >/dev/null 2>&1 || refuse "event payload does not show the owner dispatching on $REPOSITORY"

passed=1
