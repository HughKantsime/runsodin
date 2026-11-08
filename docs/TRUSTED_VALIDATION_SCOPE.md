# Trusted validation scope

`make trusted-validation-gate` runs the ten components in
`ops/release_control/inventory.json` in audited phases, reporting in CV01–CV10
order. This page says what each one
proves and what it does not. A behaviour missing from every "proves" column has
no trusted-validation evidence. It is not covered by a stage this page forgot.

## Licence posture

The release image ships unlicensed. CV04 (candidate) and CV05 (database parity)
run that exact image unchanged, so they run as **Community**.

On a Community image, every Pro route refuses with the licence error: HTTP 403,
`error.code == "permission_denied"`, and a detail containing
`Feature '<x>' requires Pro` and `Current tier: community`. CV04 and CV05 check
that refusal. They do not exercise Pro behaviour.

Pro, Education and Enterprise behaviour is proved by CV02 (contracts), in
process. The `tier_license` fixture (`tests/test_contracts/conftest.py`)
generates a throwaway Ed25519 issuer key for each test and puts it in place of
the trust root. It then writes a really signed licence that the production
verifier reads. No real licence and no release-image change is involved.

## Components

| ID | Component | Proves | Does not prove |
|----|-----------|--------|----------------|
| CV01 | release-control-local | The workflow, result-schema, installer-isolation and release-entrypoint acceptance cases (WF, RS, IN and RE in the inventory). The installer and updater run in isolated, owned Docker resources. | Product behaviour. Promotion or publishing (the controls have no publish path). |
| CV02 | contracts | About 12,600 in-process contract tests, run in batches. They cover module boundaries, RBAC, migrations, and Pro, Education and Enterprise behaviour under the substituted trust root. Backend logic for orders, products, consumables, maintenance, branding, report schedules, analytics, webhooks, SSO, permissions, groups and quotas is proved here. | Any test in `tests/test_contracts/live_environment_inventory.txt`. Those need a built image, Docker, network, an external checkout or a real model. CV02 deselects exactly that list and fails if the marked set differs from it. They run in the certification batches (`ops/run_contract_batches.sh` with `CERT_B/H/ENT_IMAGE`, `CERT_H_DOCS_BUILD`, `CERT_H_NETWORK`, `ODIN_SITE_DOCS`, `ODIN_REAL_VISION_MODEL`), with receipts, outside this gate. Also not proved: the compiled UI and the exact image. |
| CV03 | security | gitleaks, pip-audit, the npm-audit policy, bandit, semgrep, hadolint, and the operational-security and readiness/deploy-parity tests. | Runtime penetration testing. Live TLS and edge configuration. |
| CV04 | candidate | That the exact image builds and boots through its entrypoint on a fresh volume. Setup claim, seeding, personas and 2.0 RBAC (for example, models.delete is admin-only). The perimeter. WebSocket tokens and metrics. Generic upload, revision and queue lifecycles. The invoice for an existing order renders with the image's packaged fonts. Every Pro write and Pro read is refused with the licence error, and existing records stay readable. In the compiled UI (Playwright, desktop and mobile): Community gates (Pro pages, locked Integrations and Branding tabs, the Access tab open with its SSO and permission notices), the locked report-schedule form, and the free features end to end. The free features include Quick Print of a sliced 3MF (disabled, with the reason, for an unsliced one), the MFA and OIDC continuation, the IP allowlist with its lock-out guard, retention, tokens, cameras, profiles, models and printers. | Any Pro journey in the compiled UI (see "Gaps" below). Pro licence validation itself. PostgreSQL (CV04 runs on SQLite). Real printers. |
| CV05 | database-parity | On the exact image, for SQLite and twice for PostgreSQL 16: readiness, setup, personas, RBAC, CRUD, organisation scope, the relationship graph, identity paths, backup authorisation, WebSocket and background writes. Community refusal of webhooks and report schedules, and admin-only model delete. Five legacy upgrades with an idempotent second run, and refusal of malformed upgrades. Least-privilege PostgreSQL roles. The offline restore runs as `odin` on the application data volume, as the entrypoint does: wrong acknowledgement, live session, external dependency, success, rollback, and recovery from interruption. The runtime then restarts on the restored data. It also runs 140 host-side provider assertions. | Pro behaviour (Community image). The compiled UI. Restoring across versions beyond the five legacy fixtures. Managed or hosted PostgreSQL. |
| CV06 | edu-readiness | Deterministic, code-controlled EDU gates: foundation, privacy, SQLite backup/restore, hardware contracts, browser journey, load, accessibility and security. This includes the licence-context frontend unit tests that `test-edu` names. | Live TLS, legal sources and physical hardware (`make verify-edu-live`, outside this gate). A real Education licence. |
| CV07 | edu-sandbox | The EDU sandbox lifecycle and productisation contracts. A prepare, status and purge smoke on the unlicensed exact image. | The licensed sandbox lifecycle. Activation needs a normally signed, installation-bound Education licence, which is blocked externally. |
| CV08 | hardware-certification | A code-controlled replay of the four printer protocols against pinned fixtures and simulators. | Physical printers. Firmware drift. |
| CV09 | telemetry-contracts | The Telemetry V2 contract tests (`tests/test_telemetry`). | A live broker. Load. |
| CV10 | telemetry-v2-smoke | The Telemetry V2 smoke against a live MQTT broker (`tests/stress/mqtt/run_ci_smoke.sh`). | Production-scale load. |

## api_load limits for 2.0

CV06's api_load runs the fixed EDU workload (1,000 operations per repetition, 25
concurrent users, three repetitions) in process on the gate host, against SQLite,
then again in a fresh subprocess to measure memory growth.
For 2.0 three latency limits in `ops/edu_readiness/thresholds.json` were
re-baselined (decision: Hugh, 2026-10-18). Each is the measured worst plus 10-15%
headroom; the previous value and the evidence sit next to the number in
`rebaseline_2_0`. Every other limit is unchanged.

| Limit | 1.9 | 2.0 | Measured worst (gate runs and standalone, 3ae698a-a81380d) |
|---|---|---|---|
| read p95 | 500 ms | 650 ms | 591 ms |
| write p95 | 1000 ms | 1400 ms | 1232 ms |
| global p99 | 2000 ms | 2800 ms | 2478 ms |

Why 2.0 costs more per request:

- **Security and RBAC.** Every request resolves the server-enforced permission
  matrix and license tier; authentication binds the audit actor, checks the token
  blacklist and refreshes session bookkeeping (now only when stale).
- **Ledger.** A job create allocates its economic identity (cost identity rows and
  ownership checks) and charges quota atomically; reports prove ownership with
  correlated SQL.
- **Alert outbox.** A create or approval writes its alert's outbox row inside its
  transaction, then claims it and fans the alert out after the commit, so an alert
  is never lost.

What the measurements show:

- **Report tail: the tracer makes it far worse, and the rest of the cause is open.** The
  repetition-3 failures on the Mac were bursts of 5-10 usage-report queries taking
  1.5-2.8 s of wall time, against 15-20 ms normally, while executing the same amount of
  query work, with the extra time outside the query engine. api_load kept `tracemalloc`
  running through every timed repetition, and turning it off removes most of the tail.
  With garbage collection off and the tracer on, the tail stays (usage-report maximum
  5343 ms), so garbage collection doesn't explain it. Ranges over three repetitions per
  run, with two runs per setup on the Mac, in milliseconds:

  | Setup | Read p95 | Global p99 | Usage-report p99 |
  |---|---|---|---|
  | odin-ci, tracer on | 1042-2262 | 2058-3540 | 2858-4135 |
  | odin-ci, tracer off | 439-442 | 561-597 | 503-645 |
  | Mac, tracer on | 449-607 | 665-1971 | 654-2251 |
  | Mac, tracer off | 232-255 | 315-349 | 256-322 |

  A smaller tail remains without the tracer. What is known so far, all on `odin-ci`:

  - **Size.** Untraced, 4 of 30 runs in review and 1 of 20 in measuring breached 815 ms
    global p99, all in repetition 3. The worst in those runs was 1688 ms, with report p99
    up to 1811 ms. The worst seen since is 2256 ms.
  - **Not the GIL switch interval.** Setting it to 0.5 ms or 50 ms changed nothing.
  - **Request order matters somewhat.** Repetition 3's fixed order has 24 windows of 25
    operations with 6 or more reports, against 1 in repetition 1 and 12 in repetition 2.
    Giving repetition 1 that order raised traced usage-report p99 from about 2700 ms to
    about 3800 ms.
  - **Order isn't the whole cause.** With reports evenly spaced, traced repetition 3 still
    has usage-report p50 of about 2663-3261 ms, against about 670 ms in repetitions 1 and 2
    of those runs, so some state that builds up by repetition 3 also contributes.

  Timed repetitions now run with no tracer, and a tracer found running fails the gate.
  Memory growth has its own pass: the same fixture, warmup, repetitions, baseline point
  and limit, in a fresh subprocess with `tracemalloc` on. Its latencies and its FD growth
  aren't judged; the timed pass judges FD growth. A pass that crashes, hangs, writes no
  usable report or shows errors, 5xx responses or tenant leaks fails the gate. Each result
  records `latency_traced`, `memory_delta_source` and `memory_pass` in its metrics.
- **Linux, 2 vCPUs.** The same workload in the release-image container (Docker
  Desktop VM, 2 vCPUs, 4 GB, DB on the container filesystem) has no tail: report
  p95 stays at 487-627 ms in every repetition, and global p99 equals write p95.
  The baseline is about 30% slower across every class (read p95 491-578 ms), as a
  CPU-bound workload on 2 vCPUs would be.
- **The gate host is not the deployment platform.** Production and customer
  installs run Linux in Docker. These limits are a regression floor for this
  host, not a capacity statement.

Performance work continues after 2.0: shortening write transactions further,
the per-request cost of authentication and authorisation, and the report query.

## api_load limits on odin-ci

Trusted Validation moved from the M4 Mac to the `odin-ci` VM (i7-11700K, 8 vCPU) on
2026-10-30. The same workload takes about twice as long there (26-29 s per repetition
against 12-14 s), so the three latency limits moved with the host, by the same method:
measured worst plus 10-15%. The evidence sits next to each number in
`rebaseline_odin_ci`. Every other limit is unchanged. These remain a regression floor
for this host, not a capacity statement.

The latency limits are keyed by host class in `host_classes`, so the Mac keeps its own. The
class comes from what the machine reports about itself: operating system, architecture and
CPU model, plus the hostname for `odin-ci`. No setting, label or environment variable can
choose it. A machine matching no class fails api_load before any load runs, because nothing
measured there can judge it. Each api_load result records `host_class`, `latency_limits`
and `host_identity` in its metrics.

| Limit | Mac | odin-ci | Mac worst | odin-ci worst |
|---|---|---|---|---|
| read p95 | 385 ms | 750 ms | 336 ms | 652 ms |
| write p95 | 525 ms | 755 ms | 457 ms | 658 ms |
| global p99 | 525 ms | 2590 ms | 457 ms | 2256 ms |

These were re-measured on 2026-10-31, once api_load stopped timing its own tracer, by the same
method. Every earlier limit was measured with the tracer running, and the evidence is in
`rebaseline_untraced`. `maximum_repetition_seconds` went from 45 to 17, from the slower host's
worst of 14.8 s, so it sits further above the Mac's 9.2 s.

The `odin-ci` limits are all set by the open report tail: read, write, p99 and duration all come
from the same tail repetition. 3 of 35 idle runs went past 815 ms p99, and most sit near 560 ms.
Without those 3 repetitions, the worst is 478 ms read p95, 629 ms write p95, 788 ms p99 and 13.3 s.
So these limits catch less there than on the Mac until that tail is explained. The Mac's worst came from runs under normal sharing, and its limits rest on a smaller margin in
milliseconds, so a stall on that shared machine can still fail a run. Most of these runs were
api_load alone, so a first full CV06 on each host is their first test under gate conditions.

## Gaps

No stage in this gate proves the following. Read the results with these gaps in
mind.

- **Pro journeys in the compiled UI.** CV02 proves the backend logic in process.
  CV04 cannot drive these journeys, because the Community image refuses their
  writes. The following journeys were removed from
  `tests/candidate_gate/test_ui.py` for 2.0 and are in git history:
  - order production confirmation
  - order edit and invoice download
  - consumable stock adjustment
  - the report-schedule lifecycle and Run Now
  - maintenance task templates
  - product edits
  - the analytics energy widget
  - complete product cost estimates
  - product and order statistics
  - the white-label branding save, upload and removal

  Proving them again needs a licensed run of the image, which is a separate
  decision.
- **Most of the frontend unit (vitest) suite.** Only the files named in
  `test-edu` and `test-edu-privacy` run, in CV06. For example, Settings tab
  gating (`SettingsClaimAcceptance.test.tsx`) runs in no component.
- **Live-environment contract suites.** These are the tests in
  `tests/test_contracts/live_environment_inventory.txt`. They run in the
  certification batches, not here.

## Slice 1 scheduling and collision audit (2026-11-07)

The interrupted implementation in `67857a9c` ran CV02 alongside all the other
non-load gates. Its executor, schema, process-group isolation and ordered report
approach are retained, but that schedule is unsafe: contract tests such as
`test_cert_C_commerce.py` and `test_cert_C_models.py` rewrite
`frontend/src/test-fixtures/*.json`, and `Dockerfile` copies `frontend/` from the
same checkout. Distinct Docker tags alone do not isolate build inputs.

The committed inventory now describes these barriers (each bracket is a serial
worker; workers on the same row run concurrently):

| Phase | Workers | Reason |
|---|---|---|
| 1 | [CV01, CV04, CV05, CV07] alongside [CV03] | Finish Docker builds and security source scans before contracts rewrite build inputs; retain serial Docker lifecycle/cleanup. |
| 2 | [CV02] alongside [CV08, CV09] | Contracts own their fixture/receipt writes; the other worker uses separate output paths, processes, temporary directories and caches. |
| 3 | [CV10] | Fixed stress database, port and absolute `/tmp` files require exclusive use. |
| 4 | [CV06] | api_load needs an idle validation host; frontend builds and standalone security outputs also remain exclusive. |

A failed command, timeout or runner exception records a failing component and
continues to every remaining component, including the rest of that worker.
The aggregate fails if any component fails, even without a finding string.
JSON artifacts, JUnit cases, findings and console status lines use inventory
order, independent of completion order. Aggregate duration measures wall time.
Schema version, component IDs, evidence rules and all timeouts are unchanged.

### Resource audit by Make target

`native` below means `artifacts/trusted-validation/<run-id>/native`. Every outer
component has its own sanitized environment and disposable HOME, XDG config and
cache, TMPDIR/TMP/TEMP, pytest cache and disabled Python bytecode writes. Nested
`run_gate.execute` calls get their own directories too. The aggregate resolves
Playwright's installed browser path before HOME isolation; installed browser
binaries are shared read-only, with no browser installation during any phase.
Environment overrides are per subprocess, never writes to the parent environment.

| Component / target | Docker, ports and other mutable resources | Output / isolation decision |
|---|---|---|
| CV01 `release-control-local-gate` | `local_gate.py` runs acceptance tests then `installer_smoke.py`; `odin-install-smoke:<unique-run-id>` and run-owned container/network/volume, reserved loopback ports, temporary install/data directories and ownership-checked cleanup. | `native/release-control-local`, including `installer/*`. Tests use temporary fixtures. Docker build stays in phase 1. |
| CV02 `test-contracts-structured` | `structured_targets.contracts` runs eight `contract_batches` workers, each with unique basetemp/JUnit/log and disabled pytest cache. Owned test DBs, HTTP/broker/PostgreSQL fixtures remain process/test scoped; the existing exact live-environment inventory is unchanged. Certification families stay together. **Writes frontend source fixtures and application-certification receipts in the checkout.** | `native/contracts` and `batches/*`; certification captures under `artifacts/application-certification`. Cannot overlap any image build or CV06 frontend build. No other phase-2 worker reads or writes these paths. |
| CV03 `security-structured` | Operational tests, gitleaks, pip-audit, npm-audit policy, bandit, semgrep, hadolint. No image build/container. Scanners read source/lockfiles; npm audit does not install or rewrite dependencies. Scanner caches stay in the private HOME. | `native/security`, with distinct subgate files and homes. The npm exception policy can hash the entire frontend tree, so CV03 finishes before CV02 fixture writers start. CV06's standalone scanner output runs later. |
| CV04 `test-candidate` | `release_gate.runner`: `odin-candidate:candidate-<run-id>`; names prefixed `odin-candidate-`, owned network/volume; Docker-assigned loopback HTTP port. Playwright contexts/profiles temporary; no browser install. | `native/candidate/<id>`, including Playwright output. Serial with other image builders; never concurrent with fixture producers. |
| CV05 `test-database-parity` | `database_parity.runner`: `odin-dbparity:parity-<run-id>`; `odin-pg-*` and `odin-sqlite-parity-*` resources, PostgreSQL 16, isolated data volumes and container topology. Random `.odin-db-parity*` secret files in checkout, with cleanup. | `native/database-parity/<id>`. Serial Docker phase also avoids another build traversing these temporary files. |
| CV06 `test-edu-readiness` | Full existing EDU chain, including frontend/dist builds, browser servers, tmp DBs, hardware tests, api_load, accessibility and standalone security. `api_load` thresholds and repetition timeouts unchanged. | `native/edu-readiness`; legacy standalone security output remains exclusive. Runs last and alone, not concurrently with CV10 or CV02. |
| CV07 `test-edu-sandbox` | First runs sandbox/license contracts, then `edu_sandbox.certify` prepare/status/purge. Sandbox-specific image and `odin-edu-<sandbox-id>` project, networks, volumes, proxy/helper containers and loopback port. State in `.odin-edu-sandboxes/<id>`. | `native/edu-sandbox/<id>`; prerequisite JUnit uses a temporary directory. Serial Docker phase; no shared sandbox ID. |
| CV08 `test-hardware-certification` | `hardware_certification.runner` uses locked `.hardware-cert-venv` and replay fixtures/simulators; no physical printers or Docker. Simulators own ephemeral loopback listeners; test state is temporary. | `native/hardware/<id>`, distinct from CV02 captures. Serial with CV09, concurrent only with CV02. |
| CV09 `test-telemetry-contracts-structured` | In-process telemetry tests, mocked/owned transports and temporary state; no shared live broker or Docker image. | `native/telemetry-contracts`; private subprocess/cache. Serial with CV08. |
| CV10 `test-telemetry-v2-smoke-structured` | `run_ci_smoke.sh` uses ephemeral Mosquitto, but `run_one_cell.sh` wipes checkout `odin_stress.db`, uses port **8000**, and reads/writes absolute **`/tmp/odin-stress-*`** credentials/PID files. TMPDIR cannot isolate those. | `native/telemetry-smoke` and `stress-out/trusted-<run-id>`. Exclusive phase; no independent stress harness may run on this host concurrently. |

The Python process session boundary kills remaining descendants in that process
group when an outer component exits or times out, before advancing phases. Docker daemon
resources are not subprocess descendants: existing ownership cleanup remains
responsible for them. A force-killed Docker gate may leave resources behind;
inspect and clean owned residue using existing cleanup tooling before trusting
latency measurements or starting another run. No blanket Docker prune is added.
One aggregate at a time per host/checkout is an operational prerequisite; these
phases do not coordinate independent SSH sessions or other workloads. The VM
must be idle during CV06. Port bind/release races with unrelated processes remain
possible and must fail normally, never be treated as skips.

This is a conservative parallel schedule. It overlaps security with the serial Docker worker, then replay/telemetry
with contracts. No 50-minute wall-time claim is made;
CV02 alone was measured above that target. Further overlap would require isolated
source/build contexts or changing the fixture writers, and needs a new audit.

## Running the full VM precheck

On **odin-ci**, in a prepared checkout of the candidate, run:

```sh
make release-vm-precheck
# Optional explicit fresh evidence ID:
RELEASE_CONTROL_RUN_ID=vm-candidate-001 make release-vm-precheck
```

`ops/release_control/vm_precheck.sh` sets umask 022 (SSH's 0002 is not equivalent),
CI=true, DOCKER_HOST=unix:///var/run/docker.sock, and prepends the checkout's
`.venv-contracts/bin` to PATH. It invokes the same `trusted-validation-gate`
aggregate with `.venv-contracts/bin/python`. PLAYWRIGHT_BROWSERS_PATH is retained
when explicitly set, otherwise resolved to `${XDG_CACHE_HOME:-$HOME/.cache}/ms-playwright`
before HOME isolation. Every CV status prints in order and the command propagates
the aggregate failure exit. No runner service, workflow dispatch or token is
needed for this local validation entrypoint. It does not transfer the checkout,
install dependencies, reuse evidence, or create promotion authorization.

Prepare dependencies exactly as the bootstrap in `trusted-validation.yml`:
Python 3.11 `.venv-contracts` from `tests/requirements-contracts.txt`,
`.hardware-cert-venv` from backend plus hardware requirements, `.venv-stress`
from backend requirements, `npm --prefix frontend ci`, and both Python and Node
Playwright Chromium installations (with PLAYWRIGHT_SKIP_BROWSER_GC=1). Provision
Chromium's Linux system libraries. Materialize and verify the pinned Git LFS
vision model. Use the runner account's tool PATH, including Node 25.9.0, npm >=10,
Mosquitto 2.x, Docker Engine 29.x with Compose/buildx, make, git/git-lfs, gitleaks,
semgrep, hadolint, shellcheck and the Python security tools in the requirements.
A precheck on dirty source validates that source, not a committed SHA; use a clean
candidate when comparing it to a Trusted Validation run.

## odin-ci publish/promote prerequisites

All three workflows use `[self-hosted, odin-ci]`. Only host selection/bootstrap
and the publication architecture preflight change. Environments and required
approvals, manual dispatch/identity checks, least-privilege permissions, immutable
helper retrieval, evidence downloads, digests, both architecture probes, receipt
formats, rollback tags and registry-auth cleanup remain unchanged. Eligibility
still downloads and authenticates uploaded evidence even on the same host.

- Linux x86_64 runner with Python 3.11 on PATH (including `jsonschema` and its
  dependencies for helper imports), `gh`, git/git-lfs, bash and standard Unix
  tools (`curl`, `jq`, `find`, `cmp`, `awk`, `sed`, `tar`, `df`). Actions must be
  able to create fresh private directories in RUNNER_TEMP and the workspace.
- Docker socket access at `/var/run/docker.sock` for the runner user. Docker
  Engine 29.x, Compose v2 for validation, and a discoverable buildx CLI plugin.
  The existing pinned setup-buildx action configures the per-run builder;
  preflight checks the host plugin before installing that builder. Ensure the
  plugin remains discoverable when DOCKER_CONFIG points at a fresh auth folder
  (install it system-wide, not solely under the user's `.docker/cli-plugins`).
- **Working linux/arm64 execution**, via host-provisioned binfmt/QEMU, plus native
  amd64. Publication still builds both platforms and boots each exact manifest;
  production's disposable checker still boots both. The prior VM arm64 health
  failure is unresolved host risk. A small pinned Python arm64 preflight cannot
  establish full-image health; the existing full-image checks remain mandatory.
  No privileged binfmt installation or automatic provider fallback is added.
- GHCR access for `ghcr.io/hughkantsime/odin`. The existing per-job `GITHUB_TOKEN`
  supplies packages:write to publication/production and actions:read/contents:read
  for prerequisite evidence. The repository must have package access. Login is
  `docker login --password-stdin` with isolated DOCKER_CONFIG and existing logout
  cleanup; no persistent personal login is required. `gh` uses that job token.
- Network/DNS/CA access to GitHub APIs, Actions artifacts, GHCR, Git LFS objects,
  base-image registries and build dependency registries. **Cosign is not used by
  these workflows**; no signing tool/key requirement is introduced. Digest and
  receipt verification is performed by the existing Python helpers/buildx.
- Disk: reserve space on **both** the workspace/RUNNER_TEMP filesystem and Docker's
  data-root for two architecture builds, BuildKit cache, pulled probe images,
  checkout/LFS assets, volumes and evidence. A planning floor is 50 GiB free in
  Docker storage plus 20 GiB on the workspace filesystem, with more for retained
  caches; these are capacity estimates, not a measured peak or a gate threshold.
  Inspect `df -h` and `docker system df` before the first release. Cleanup stays
  scoped to owned resources; do not prune another run's data.
- One trusted runner job at a time, no overlapping manual aggregate/stress run,
  and the existing root-owned runner job-started hook installed and enforced as
  described in `ops/ci/README.md`. Co-locating candidate execution and publication
  removes the prior cross-host trust boundary: keep this host restricted to the
  owner's approved dispatches, with no ambient deployment credentials. Workflow
  isolation and authenticated evidence still apply; they are not a VM reset.

The workflow files must land on main before dispatching that release chain.
This checkout change alone does not provision the host, prove the historic CV02
failure reproduces under SSH, or demonstrate a successful multi-architecture
publish/promote. Those remain live acceptance checks on odin-ci.
