# ODIN Blocker Cleanup Spec

## Purpose

Move ODIN from "advanced but messy" to a clean, resumable operating state across the core backend, marketing site, native apps, and MCP package.

This is not a feature expansion plan. It is a stabilization spec: fix the known blockers, make the current state truthful, and leave every repo with clear CI, deploy, and release signals.

## Current Snapshot

Observed on 2026-05-21 from local repos, production health probes, and GitHub Actions.

### Core backend: `HughKantsime/runsodin`

- Local repo: `odin`, branch `main`.
- Production health: `https://odin.subsystem.app/health` returns `status=ok`, `version=1.9.7`.
- Latest GitHub Actions on `main`: `Deploy` and `Security & Code Quality` green for commit `d9bd104`.
- `VERSION` is `1.9.7`.
- `frontend/package.json` still reports `1.9.6`, which is version drift.
- Latest tag state is messy: `v1.9.7` points to the release commit, while a follow-up SQL fix commit sits after it.
- Local untracked file: `AGENTS.md`.
- Main product state: Bambu telemetry V2 is implemented behind `ODIN_TELEMETRY_V2`; production still needs real-printer cutover validation before legacy deletion.

### Marketing/docs site: `HughKantsime/odin-site`

- Local repo: `odin-site`, branch `odin-145-mqtt-scalability-proof`, not `main`.
- Production `https://runsodin.com` returns 200 and last-modified headers from 2026-05-02.
- Latest `ODIN Sync` workflow failed on 2026-05-04.
- Failure cause: Semgrep blocking finding in `scripts/composite-reddit-thumb.py` at `urllib.request.urlopen(req, timeout=15)`.
- `npm audit --audit-level=high` also reports moderate vulnerabilities: PostCSS `<8.5.10` and `uuid <14` through `svix` / `resend`, but those were not the terminal failure in the observed run.
- Local untracked files:
  - `public/launch-images/discord-welcome-1920x1080.png`
  - `public/launch-images/hn-card-v2-1200x630.png`
  - `public/launch-images/og-default-1200x630.png`
- Local branch includes unmerged ODIN-145 launch/scalability content.

### Native apps: `HughKantsime/odin-native`

- Local repo: `odin-native`, branch `main`.
- Latest `Security & Code Quality` workflow green.
- Latest `Build & Deploy` workflow red.
- Build failure cause: iOS UI smoke test cannot install `ODINWidgets.appex` in the simulator because the app extension placeholder is missing `bundleVersion`.
- Daily `ASC Review Status` workflow red repeatedly through 2026-05-20.
- ASC probe failure cause: Python 3.14 / urllib TLS certificate verification failure against App Store Connect: `CERTIFICATE_VERIFY_FAILED unable to get local issuer certificate`.
- Local untracked directory: `release/v1.0/screenshots/mac-broken/`.

### MCP package: `HughKantsime/odin-mcp`

- npm package `odin-print-farm-mcp` is published at `2.1.0`.
- Latest GitHub Actions for `main` green.
- Local tests pass: `26 passed`.
- Backend agent-scope/dry-run focused tests pass: `23 passed`.
- Local repo has uncommitted changes that appear to be the v2.1.0 docs/tool/test alignment:
  - `CHANGELOG.md`
  - `README.md`
  - `package-lock.json`
  - `package.json`
  - `skills/odin-farm/SKILL.md`
  - `src/tools/write_tools.ts`
  - untracked `test/write_tools.test.ts`
  - untracked `excalidraw.log`

## Non-Goals

- Do not redesign ODIN.
- Do not add new feature surfaces.
- Do not delete the Bambu legacy adapter until V2 has been validated on real hardware.
- Do not build Docker images on production.
- Do not bypass CI by editing deployed containers or Vercel output directly.
- Do not create PRs unless explicitly requested; current project pattern is direct `main` pushes.

## Success Criteria

The cleanup is complete only when all of these are true:

- `runsodin` production still returns healthy `1.9.x` or newer after cleanup.
- `runsodin` latest `Deploy` and `Security & Code Quality` runs are green on `main`.
- `odin-site` latest `ODIN Sync` run is green and production content is not stale relative to the latest backend release.
- `odin-native` latest `Build & Deploy`, `Security & Code Quality`, and `ASC Review Status` runs are green or intentionally non-blocking with a documented reason.
- `odin-mcp` local working tree is clean except ignored scratch files, npm package state matches repo state, and tests pass.
- Every repo is on an intentional branch with no unexplained untracked files.
- Version metadata is consistent enough that a future release can be cut without guessing which artifact is canonical.
- The Bambu telemetry V2 cutover status is documented as one of:
  - not started,
  - staging validated,
  - production validated,
  - rolled back with reason.

## Workstreams

### 1. Core Backend Cleanup

#### Problem

The backend is deployed and green, but release metadata is inconsistent:

- `VERSION` says `1.9.7`.
- `frontend/package.json` says `1.9.6`.
- `v1.9.7` tag is not at latest `main`.
- The changelog says v1.9.7 includes the SQL/nosemgrep fix, but one SQL fix commit landed after the release commit.

This makes future release decisions ambiguous.

#### Required Fix

1. Decide whether latest `main` should become:
   - a corrected `v1.9.7` tag, if retagging is acceptable for this project, or
   - a new patch release, e.g. `1.9.8`, if immutable tags are preferred.
2. Align version sources:
   - `VERSION`
   - `frontend/package.json`
   - any generated service worker/version metadata controlled by the existing bump script.
3. Run the existing version bump path instead of hand-editing version files:
   - use `ops/bump-version.sh` or the established `make release` path.
4. Preserve the current production deploy route:
   - commit to repo,
   - tag/push via release workflow,
   - GHCR build,
   - production pulls image.

#### Acceptance Checks

- `git status --short` is clean except intentionally ignored local files.
- `cat VERSION` and `node -p "require('./frontend/package.json').version"` agree.
- `gh run list --repo HughKantsime/runsodin --limit 5` shows green latest deploy and security runs.
- `curl -fsS https://odin.subsystem.app/health` returns `status=ok` and the expected version.

### 2. Telemetry V2 Real-Hardware Cutover

#### Problem

Telemetry V2 is implemented and heavily tested, but the actual operator cutover is not complete. Legacy Bambu paths still exist by design. The current state is good engineering, but unresolved operationally.

#### Required Fix

1. Keep legacy fallback in place until real hardware validates V2.
2. Flip `ODIN_TELEMETRY_V2=1` in staging or an equivalent hardware test deployment.
3. Validate against a real Bambu printer:
   - live status returns correct state, temperatures, progress, AMS, and HMS fields,
   - pause/resume/stop works,
   - AMS sync works,
   - dispatch can upload/start a small print,
   - monitor daemon writes telemetry and alerts without log spam.
4. If staging passes, flip production with explicit rollback notes.
5. Do not delete legacy code until production has been stable long enough that rollback is not the likely path.

#### Acceptance Checks

- `ODIN_TELEMETRY_V2=1` smoke evidence is captured in a doc or session note.
- Production or staging logs show V2 monitor active without repeated import/path failures.
- A real printer command is verified end-to-end, not only through mock tests.
- `backend/modules/printers/telemetry/CUTOVER.md` is updated with actual status.

### 3. Marketing Site Sync Repair

#### Problem

The latest backend release dispatch failed to sync `odin-site`. The site is live, but the automated release-to-site loop is not healthy.

Observed terminal failure:

```text
scripts/composite-reddit-thumb.py
python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
line 61: urllib.request.urlopen(req, timeout=15)
```

There are also audit warnings for PostCSS and uuid/svix/resend that should be handled instead of ignored.

#### Required Fix

1. Return local checkout to an intentional state:
   - decide whether `odin-145-mqtt-scalability-proof` should merge to `main`, be parked, or remain active,
   - account for the three untracked launch images.
2. Fix the Semgrep finding properly:
   - constrain allowed URL schemes and hosts before `urlopen`, or
   - replace the dynamic fetch with a local/static asset path if the script only needs committed launch assets.
3. Address dependency audit:
   - upgrade PostCSS path to `>=8.5.10`,
   - inspect `resend` / `svix` / `uuid` upgrade path,
   - avoid a breaking downgrade/force audit fix without checking API compatibility.
4. Re-run the `ODIN Sync` workflow or manually dispatch it after the fix.
5. Confirm production alias points to the new successful deployment.

#### Acceptance Checks

- `npm test` passes in `odin-site`.
- `npm run build` passes in `odin-site`.
- `npm audit --audit-level=high` is clean or only reports explicitly triaged non-high findings.
- Semgrep workflow passes without broad suppressions.
- Latest `ODIN Sync` workflow is green.
- `curl -I https://runsodin.com` shows a fresh deployment timestamp or matching asset hash evidence.

### 4. Native Build Pipeline Repair

#### Problem

Native CI security is green, but build/deploy is red. The iOS UI test cannot install the app because `ODINWidgets.appex` has invalid placeholder attributes:

```text
bundleVersion must be set in placeholder attributes for an app extension placeholder
```

#### Required Fix

1. Ensure `CFBundleVersion` is set for `ODINWidgets` in simulator/debug builds and release builds.
2. Do this at the project generation level, not by patching generated Xcode project output.
3. Confirm the widget extension inherits or explicitly receives:
   - `MARKETING_VERSION`
   - `CURRENT_PROJECT_VERSION`
4. Add an invariant if practical so future target additions cannot omit bundle version metadata.
5. Re-run native build workflow.

#### Acceptance Checks

- `make build` passes locally or on the runner.
- The iOS UI smoke test installs and launches the app on simulator.
- Latest `Build & Deploy` workflow is green.
- The app archive still embeds `ODINWidgets.appex` for App Store builds.

### 5. ASC Review Probe Repair

#### Problem

The daily App Store Connect probe is failing before it reaches ASC response handling:

```text
ssl.SSLCertVerificationError: unable to get local issuer certificate
```

This means the monitoring signal is currently false noise.

#### Required Fix

1. Stop using whichever Python runtime lacks a working CA bundle on the runner, or explicitly configure certifi.
2. Prefer a deterministic runtime:
   - `python3.11` with known cert path, or
   - install/use `certifi` and pass an SSL context to urllib, or
   - switch the probe to `curl`/`gh api` style transport with verified system certificates.
3. Preserve certificate verification. Do not disable TLS verification.
4. Make the workflow distinguish:
   - transport failure,
   - ASC API auth failure,
   - actual App Review state change.

#### Acceptance Checks

- Manual `workflow_dispatch` of `ASC Review Status` succeeds.
- Scheduled run succeeds when state is unchanged.
- If ASC state has changed, the workflow fails intentionally with the state-change annotation, not a transport traceback.
- Artifact contains `status.json` with review submission, app store version, latest build, and probe timestamp.

### 6. MCP Repo Reconciliation

#### Problem

The npm package is published at `2.1.0` and tests pass, but local repo state is dirty. Some local changes duplicate the published v2.1.0 state and may not be committed/tagged consistently.

#### Required Fix

1. Compare local diff against `origin/main` and npm `2.1.0`.
2. Keep real source/test/doc improvements.
3. Drop scratch files such as `excalidraw.log`.
4. If the local source differs from published npm, decide whether to:
   - commit as `2.1.1`, or
   - reset local-only duplicate changes if they are already represented in the published package.
5. Ensure package allowlist includes only intended files.

#### Acceptance Checks

- `npm test` passes.
- `npm run build` passes.
- `npm pack --dry-run` includes expected files and excludes scratch.
- Local `git status --short` is clean.
- npm version and repo tag tell the same story.

### 7. Workspace Hygiene

#### Problem

Current local state has unexplained untracked files in multiple repos and one site repo on a feature branch. This makes future work error-prone.

#### Required Fix

For each ODIN repo:

1. Record current branch.
2. Record untracked/modified files.
3. Classify each item:
   - commit,
   - move to artifact/scratch,
   - add to `.gitignore`,
   - delete after verification.
4. End on either:
   - clean `main`, or
   - intentionally dirty feature branch with a written reason.

#### Acceptance Checks

- `git status --short` is empty in repos considered ready.
- Any intentionally dirty repo has a short note in the cleanup summary.
- No generated secrets, screenshots, or scratch logs are staged accidentally.

## Execution Order

1. Clean and reconcile `odin-mcp`, because it is contained and already test-green.
2. Fix `odin-site` sync, because the public marketing/docs surface is stale relative to backend releases.
3. Fix `odin-native` build and ASC probe, because both are CI signal problems and not product architecture changes.
4. Align backend version/tag metadata and cut a clean patch release if needed.
5. Run telemetry V2 staging cutover as a deliberate operator action.
6. Update docs/session memory after the cleanup, including drift corrections in the ODIN wiki pages.

## Verification Matrix

| Area | Command / Probe | Expected |
|---|---|---|
| Backend health | `curl -fsS https://odin.subsystem.app/health` | `status=ok`, expected version |
| Backend CI | `gh run list --repo HughKantsime/runsodin --limit 5` | latest deploy + security green |
| Site build | `npm test && npm run build` in `odin-site` | pass |
| Site sync | `gh run list --repo HughKantsime/odin-site --workflow "ODIN Sync" --limit 1` | green |
| Native build | `gh run list --repo HughKantsime/odin-native --workflow "Build & Deploy" --limit 1` | green |
| ASC probe | `gh workflow run asc-review-status.yml` then inspect latest run | green or intentional state-change failure |
| MCP | `npm test && npm run build` in `odin-mcp` | pass |
| Git hygiene | `git status --short` in each repo | clean or documented |

## Risks

- Retagging `v1.9.7` could confuse any consumer that already pulled the old tag. Prefer a new patch release unless there is a strong reason to rewrite the tag.
- Telemetry V2 mock/fixture confidence is high, but command paths still need real printer validation. Do not delete legacy before that.
- `npm audit fix --force` in `odin-site` may downgrade or break `resend`; inspect the dependency graph instead of force-applying.
- Native simulator failures can be Xcode-version-specific. Fix target metadata first; only then consider runner/simulator cleanup.
- ASC probe must not solve TLS failure by disabling verification.

## Deliverables

- One cleanup commit per repo where possible, with focused messages.
- A short final cleanup report containing:
  - versions,
  - CI run links,
  - production health output,
  - telemetry V2 status,
  - remaining intentional non-clean state, if any.
- A session-wrap update after meaningful work, so the vault stops claiming stale versions and obsolete known issues.
