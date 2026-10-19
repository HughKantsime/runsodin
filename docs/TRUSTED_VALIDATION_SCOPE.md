# Trusted validation scope

`make trusted-validation-gate` runs the ten components in
`ops/release_control/inventory.json` in order. This page says what each one
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
concurrent users, three repetitions) in process on the gate host, against SQLite.
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

- **macOS SQLite locking tail.** The repetition-3 failures are bursts of 5-10
  usage-report queries taking 1.5-2.8 s of wall time, against 15-20 ms normally,
  while executing the same amount of query work. The extra time is spent
  outside the query engine. It remains with the database on a RAM disk, and it
  disappears on Linux, which fits macOS's file-locking path under concurrent
  connections.
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
