#!/usr/bin/env bash
# Job-started hook for the odin-ci runner, set through ACTIONS_RUNNER_HOOK_JOB_STARTED in the
# runner's .env. Jobs here reach root through Docker, can read the runner's credentials and
# later jobs' tokens, so the runner only runs what the owner dispatched on this repository.
# A non-zero exit stops the job before any of its steps.
set -euo pipefail
OWNER=HughKantsime
OWNER_ID=201174638
REPOSITORY=HughKantsime/runsodin
REPOSITORY_ID=1143865575

refuse() { echo "odin-ci refuses this job: $*" >&2; exit 1; }

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
