# odin-ci runner

| Work | Host | Why |
|---|---|---|
| Trusted Validation | odin-ci | Production's OS and architecture, off the dev Mac |
| Eligibility, publish, production promotion, rollback | Mac | arm64 under QEMU never goes healthy on the VM, and eligibility authenticates validation's evidence, so it stays off the host that just ran candidate code |

Evidence moves between hosts only as an uploaded artifact. Neither host reads the other's disk. The Mac isn't being retired.

## Rebuild

1. Create the VM on pve3 (Ubuntu 24.04, 8 vCPU, 200 GB). It has 23 GiB of RAM today.
2. Run `provision-odin-ci.sh`, then `provision-odin-ci-tools.sh`. Both can be re-run.
3. On pve3: `zfs set sync=disabled rpool/data/vm-130-disk-0`. CV02's SQLite commits need it, and the disk only holds state that can be rebuilt.
4. Copy Bambu Studio's machine profiles; `provision-odin-ci-tools.sh` prints the command.
5. Register the runner, then start the service. This host has no `gh`, so the token comes from the Mac, straight from `gh` to `config.sh`, and is never printed:
   ```sh
   gh api -X POST repos/HughKantsime/runsodin/actions/runners/registration-token --jq .token \
     | ssh odin@192.168.68.151 'cd ~/actions-runner-runsodin && xargs -I{} ./config.sh --unattended \
         --url https://github.com/HughKantsime/runsodin --token {} --name odin-ci-pve3 --labels odin-ci --work _work \
         && sudo ./svc.sh install odin && sudo ./svc.sh start'
   ```
6. Install the job hook, then restrict egress (both below).

The runner's `.credentials` and `.credentials_rsaparams` are long-lived, and a job that gets root can read them. That is why step 6 matters.

## Job hook

`runner-job-started-hook.sh` ends any job that isn't an owner `workflow_dispatch` on HughKantsime/runsodin, and any job it can't check: no `jq`, no event file, a missing variable. A failed hook alone wouldn't stop anything, because the runner treats a hook as an ordinary step and later `if: always()` steps still run. So on refusal it checks that its parent's process name is `Runner.Worker`, then sends it SIGKILL.

It belongs on both runners. The Mac needs it more: a job there runs in the account holding the gh login and the production SSH key.

To install, from a checkout of this repository, as the runner's account, while no runner on that host has a job. On odin-ci, as `odin`; on the Mac, as `ollama`, where `sudo` asks for a password:

```sh
ops/ci/install-runner-hook.sh
```

It copies the hook to `/usr/local/libexec/odin-ci/`, root-owned, sets `ACTIONS_RUNNER_HOOK_JOB_STARTED` in the runner's `.env` (`~/actions-runner-runsodin`, or `RUNNER_DIR`), and restarts that runner, which reads `.env` only at start. The hook only knows this repository, so it goes in this repository's runner alone. Until it is live, never approve a fork PR's run.

### Prove it

Run this on each host after installing. It needs a real runner, so nothing in the test suite can stand in for it. Push this workflow to a throwaway branch: a `push` event is refused, and nothing else about the hook has to change.

```yaml
# .github/workflows/hook-proof.yml on branch hook-proof
name: Hook proof
on: push
jobs:
  proof:
    runs-on: [self-hosted, odin-ci]   # on the Mac: [self-hosted, mac-mini-runner, m4]
    steps:
      - run: echo "HOOK PROOF FAILED, a step ran"
      - if: always()
        run: echo "HOOK PROOF FAILED, an always() step ran"
```

It passes if all of these hold:

- The run fails.
- On that host, the last line of `~/.odin-runner-job-hook.log` names the proof run's `run=` id and says `killing Runner.Worker`. The hook writes this before the kill, because the kill also stops the runner uploading the step's log, so "Set up runner" may show nothing.
- `gh run view <id> --log` has no `HOOK PROOF FAILED` line.
- `Runner.Listener` is still up: `systemctl is-active actions.runner.HughKantsime-runsodin.odin-ci-pve3.service` on odin-ci, `launchctl list | grep runsodin` on the Mac.

Then delete the branch, and confirm the next owner dispatch of Trusted Validation gets past "Set up runner".

### Keep it from drifting

Trusted Validation's bootstrap checks the hook on odin-ci:

| `ACTIONS_RUNNER_HOOK_JOB_STARTED` | `ODIN_RUNNER_HOOK_REQUIRED` | Result |
|---|---|---|
| any | anything but `true` or empty | Fails, so a typo like `TRUE` can't turn the check off |
| set | `true` or empty | Fails unless it's exactly `/usr/local/libexec/odin-ci/runner-job-started-hook.sh`, a regular file that only root can write, in a directory that only root can write |
| not set | `true` | Fails |
| not set | empty or not set | Warns on the run, and goes on |

The variable is a repository variable, so nothing fails before the hook is installed. It survives a VM rebuild, which is when a hook is most likely to go missing, and a job can't change it. Set it as the last step of installing on odin-ci, once the proof passes:

```sh
gh variable set ODIN_RUNNER_HOOK_REQUIRED --body true --repo HughKantsime/runsodin
```

The check only sees odin-ci. No workflow checks the Mac's hook yet.

### Changing the hook

The step after checkout compares the installed hook with the candidate's `ops/ci/runner-job-started-hook.sh`, byte for byte. A candidate that changes the hook therefore fails validation until the new copy is installed, so the order is:

1. Wait until no job is running on odin-ci.
2. From a checkout of that candidate, run `ops/ci/install-runner-hook.sh`.
3. Dispatch validation for that candidate.

Until that candidate reaches main, any other candidate fails the same comparison, because it still carries the old hook. Rolling back to a candidate with an older hook needs the older copy installed the same way. A candidate from before the hook existed has no file to compare, so it fails too.

### Remove what eligibility left on the running VM

Provisioning no longer installs `gh` or a user-site jsonschema, but the running VM still has both. On odin-ci, as `odin`:

```sh
python3.11 -m pip uninstall -y --break-system-packages jsonschema jsonschema-specifications referencing rpds-py
python3.11 -m pip uninstall -y --break-system-packages pip
sudo apt-get purge -y gh
```

The second command removes the user-site pip that only those installs needed.

To check:
- `command -v gh` prints nothing.
- `python3.11 -m pip list --user` fails.
- `python3.11 -c 'import jsonschema; print(jsonschema.__file__)'` no longer prints a path under `~/.local`. It still resolves, at `/usr/lib/python3/dist-packages`, because Ubuntu's own python3-jsonschema is a cloud-init dependency and stays.

## Moving validation back to the Mac

Use this when the host is the problem and a release can't wait for it: the chain fails with "still hadn't started", or odin-ci is down or unusable for too long. If the candidate failed validation on its merits, fix the candidate instead.

Don't just change the `runs-on` label. With `ODIN_RUNNER_HOOK_REQUIRED` set to `true` and no hook on the Mac, the job stops at the hook check. With the variable cleared, it stops at the first `command -v`, because the Mac runner's `.path` is `/usr/bin:/bin:/usr/sbin:/sbin` and this workflow doesn't add Homebrew.

Branch `revert/validation-to-mac` holds one commit, which restores the Mac's `trusted-validation.yml` and the tests that pin it. From a clean checkout on the Mac, with Homebrew on `PATH`:

1. Check that no release is running. `~/.odin-release-chain.lock` must not exist, and `gh run list --repo HughKantsime/runsodin --status in_progress` must show no validation, publish or promotion. Cancel a validation still queued for odin-ci with `gh run cancel <run> --repo HughKantsime/runsodin`.
2. Apply the commit:
   ```sh
   git fetch origin
   git switch -c revert-validation origin/main
   git cherry-pick revert/validation-to-mac
   ```
3. Run its tests: `python3.11 -m pytest tests/test_release_control -q`
4. With the owner's approval, run `git push origin HEAD:main`. This is the step that moves validation: the chain dispatches with `--ref main`, so it doesn't need a release.
5. Clear the variable, which the restored workflow doesn't read: `gh variable delete ODIN_RUNNER_HOOK_REQUIRED --repo HughKantsime/runsodin`
6. Rebase the candidate on the new main, push it as `release-candidate/<SHA>`, and start the chain without `VR=` or `PB=`, because the evidence has to be for the new SHA.

While validation is on the Mac:

- Its evidence comes from the dev Mac, not from production's OS and architecture, as it did before the move to odin-ci. The table at the top of this file is wrong.
- The Mac's latency limits apply by themselves, and the manifest stops carrying `api_load_host`.
- `release-chain-v3.sh` works unchanged. Its odin-ci disk line may log "unavailable", which is harmless.
- Nothing checks a runner hook on either host.

To move back, `git revert` that commit on main and rebase any candidate on it. Then repeat [Prove it](#prove-it) and set the variable again as in [Keep it from drifting](#keep-it-from-drifting).

## Egress to production

odin-ci shares a /22 with production and needs nothing from it: no CV04-CV08 source names production's address or hostname, which `tests/test_release_control/test_runner_host_tools.py` checks.

`egress-to-prod.sh` is an accident guard only. It stops a test or a stray config reaching production by mistake, but a job with root through Docker can delete the table. A real boundary belongs on pve3 or the gateway.

Only a real run proves nothing needs production, so the rule goes on in two stages:

```sh
ops/ci/egress-to-prod.sh audit     # counts, doesn't block
# run one full Trusted Validation
ops/ci/egress-to-prod.sh check     # fails if anything was counted
ops/ci/egress-to-prod.sh enforce   # rejects from now on, across reboots
```

This blocks production's LAN address only. The public hostname stays reachable, as it is for anyone, and this host holds no production credential.

## Ad-hoc runs

`vm-guarded-run.sh <command...>` refuses while the runner has a job and stops the command if one starts. Use it for anything run by hand here: a test run during a job skews its latency limits and can collide with its ports.
