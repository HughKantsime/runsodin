# shellcheck shell=bash
# Shared by the odin-ci runner scripts. Source it; it does nothing by itself.
# Bash 3.2 compatible, so its tests also run on the Mac.
RUNNER_DIR="${RUNNER_DIR:-$HOME/actions-runner-runsodin}"

# The runner keeps Runner.Listener for as long as it is up and starts Runner.Worker only for
# a job. This matches the program name in the process list; `pgrep -f` would also match the
# shell running it. If the list can't be read, a job counts as active.
runner_job_active() {
  local listing
  listing=$(ps -eo args=) || return 0
  grep -Eq '^([^ ]*/)?Runner\.Worker( |$)' <<<"$listing"
}

# Set KEY=VALUE in the runner's .env. Returns 0 if the file changed, 1 if it already said that.
runner_env_set() {
  local key="$1" value="$2" file="$RUNNER_DIR/.env" current="" kept
  [ ! -f "$file" ] || current=$(grep -E "^$key=" "$file" || true)
  [ "$current" != "$key=$value" ] || return 1
  kept=""
  [ ! -f "$file" ] || kept=$(grep -Ev "^$key=" "$file" || true)
  { [ -z "$kept" ] || printf '%s\n' "$kept"; printf '%s=%s\n' "$key" "$value"; } > "$file.new"
  mv "$file.new" "$file"
}

# The runner reads .env only when it starts. Refuses during a job rather than killing it.
runner_restart() {
  local unit
  if [ ! -f "$RUNNER_DIR/.service" ]; then
    echo "runner is not registered yet; it reads .env when it first starts"
    return 0
  fi
  unit=$(cat "$RUNNER_DIR/.service")
  if runner_job_active; then
    echo "a runner job is active; .env is written but not live. After the job: sudo systemctl restart $unit" >&2
    return 1
  fi
  sudo systemctl restart "$unit"
}
