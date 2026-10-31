# odin-ci runner

| Work | Host | Why |
|---|---|---|
| Trusted Validation | odin-ci | Production's OS and architecture, off the dev Mac |
| Stage eligibility (`promote.yml`, action `stage`) | odin-ci | Only reads evidence through the API and uploads an artifact |
| Publish, production and demo eligibility, production promotion, rollback | Mac | arm64 under QEMU never goes healthy on the VM |

Evidence moves between hosts only as an uploaded artifact. Neither host reads the other's disk.

## Rebuild

1. Create the VM on pve3 (Ubuntu 24.04, 8 vCPU, 200 GB). It has 23 GiB of RAM today.
2. Run `provision-odin-ci.sh`, then `provision-odin-ci-tools.sh`. Both can be re-run.
3. On pve3: `zfs set sync=disabled rpool/data/vm-130-disk-0`. CV02's SQLite commits need it, and the disk only holds state that can be rebuilt.
4. Copy Bambu Studio's machine profiles; `provision-odin-ci-tools.sh` prints the command.
5. Register the runner, then start the service. The token goes straight from `gh` to `config.sh` and is never printed:
   ```sh
   cd ~/actions-runner-runsodin
   gh api -X POST repos/HughKantsime/runsodin/actions/runners/registration-token --jq .token \
     | xargs -I{} ./config.sh --unattended --url https://github.com/HughKantsime/runsodin --token {} \
         --name odin-ci-pve3 --labels odin-ci --work _work
   sudo ./svc.sh install odin && sudo ./svc.sh start
   ```
6. Install the job hook, then restrict egress (both below).

The runner's `.credentials` and `.credentials_rsaparams` are long-lived, and a job that gets root can read them. That is why step 6 matters.

## Job hook

`runner-job-started-hook.sh` fails any job that isn't an owner `workflow_dispatch` on HughKantsime/runsodin, before its steps run. To install, on odin-ci as `odin`, from a checkout of this repository:

```sh
ops/ci/install-runner-hook.sh
```

It refuses during a job, copies the hook to `/usr/local/libexec/odin-ci/`, sets `ACTIONS_RUNNER_HOOK_JOB_STARTED` in the runner's `.env` and restarts the service, because the runner reads `.env` only at start. Afterwards, dispatch a validation and look for "Set up runner" passing the hook; to prove the refusal, run the hook by hand with `GITHUB_EVENT_NAME=pull_request`. Until the hook is live, never approve a fork PR's run.

## Egress to production

odin-ci shares a /22 with production and needs nothing from it: no CV04-CV08 source names production's address or hostname, which `tests/test_release_control/test_runner_host_tools.py` checks. Only a real run proves it, so the restriction goes on in two stages:

```sh
ops/ci/egress-to-prod.sh audit     # counts, doesn't block
# run one full Trusted Validation
ops/ci/egress-to-prod.sh check     # fails if anything was counted
ops/ci/egress-to-prod.sh enforce   # rejects from now on, across reboots
```

This blocks production's LAN address only. The public hostname stays reachable, as it is for anyone, and this host holds no production credential.

## Ad-hoc runs

`vm-guarded-run.sh <command...>` refuses while the runner has a job and stops the command if one starts. Use it for anything run by hand here: a test run during a job skews its latency limits and can collide with its ports.
