# Execution Plan — Agent-Native ODIN

**Track ID:** `agent-native-odin_20260414`
**Created:** 2026-04-14
**Total tasks:** 34 across 6 phases

## Overlap Check

Reviewed `conductor/tracks.md` completed entries. No overlap with:
- `user-friction-and-fail-loud-gaps_20260414` (v1.8.8 ops UX — different surface)
- `kill-the-stubs-and-native-positioning_20260413` (v1.8.5 Spoolman/digest — different surface)
- `codex-adversarial-remediation_20260412` (v1.8.2–v1.8.4 security — will reuse `safe_post` SSRF primitive for R12)
- `modular-architecture-refactor_20260226` (module registry is how we'll hang the new middleware)
- `route-splits_20260226` (will follow the same split convention for new middleware files)

Reuses but does not duplicate: `safe_post` (for ITAR mode check), module registry pattern, API key model, audit log writer, scheduler/background task infra.

## Phase 1 — ODIN Backend Agent Primitives

**Dependency:** none (foundation layer).

- [x] **T1.1 — Migration 005: idempotency_keys table**
  - Created `backend/core/migrations/005_idempotency_keys.sql`.
  - `CREATE TABLE IF NOT EXISTS idempotency_keys` + `ix_idempotency_keys_created_at` index.
  - **Schema discovery (2026-04-14):** existing `api_tokens` table already has `scopes TEXT DEFAULT '[]'` (JSON array). No `ALTER TABLE` needed. T1.4 uses well-known scope strings (`"agent:read"`, `"agent:write"`) in the existing array instead of a new column.
  - Contract test `tests/test_contracts/test_migration_005_idempotency_keys.py` — 8/8 passing (file-exists, applies-cleanly, columns-match, composite-PK, index-exists, idempotent, roundtrip, per-user-PK-scope).

- [x] **T1.2 — Idempotency middleware**
  - Created `backend/core/middleware/__init__.py` + `backend/core/middleware/idempotency.py`.
  - Wired via `app.middleware("http")(idempotency_middleware)` in `_register_http_middleware` — registered last so it's outermost and can short-circuit before auth.
  - Request hash canonicalizes JSON key-order. Non-JSON body hashes verbatim. Max cached body 256 KB.
  - Hourly prune added to existing `_periodic_cleanup` task in `core/app.py` (guarded so missing table doesn't break cleanup on older deployments).
  - Contract test `tests/test_contracts/test_idempotency_middleware.py` — 10/10 passing (miss, hit roundtrip, conflict, per-user scope, expiry at read, prune, hash canonicalization × 4).

- [ ] **T1.3 — Dry-run header support**
  - New `backend/core/middleware/dry_run.py` — reads `X-Dry-Run: true`, sets `request.state.dry_run = True`.
  - Enumerated list of `DRY_RUN_SUPPORTED_ROUTES` in each module's routes file — explicit opt-in per route.
  - Each route checks `if request.state.dry_run: return {"dry_run": True, "would_execute": preview_dict}` **before** any DB commit / event emit / external call.
  - Per-route handling — NO global rollback (rationale in spec Tech Notes).
  - Start with queue ops, printer control, spool ops, maintenance, alert ops, order notes. Do NOT wire auth/license/backup/user/admin/SMTP endpoints.
  - **Acceptance:** contract test `test_dry_run.py` — pick one endpoint per category above, assert no DB row created when `X-Dry-Run: true`, response includes `would_execute`.

- [ ] **T1.4 — Agent-scoped API tokens**
  - Existing `api_tokens.scopes` is already a JSON-array column. Define two new well-known scope strings: `agent:read` and `agent:write`.
  - New `require_scope(*allowed: str)` dependency in `backend/core/dependencies.py`, composable with existing `require_role`. Parses the token's `scopes` JSON, checks intersection with `allowed`.
  - Wire on agent-surface endpoints (Phase 2's tool list): read endpoints accept any of `{admin, agent:read, agent:write}`; write endpoints accept any of `{admin, agent:write}`. Auth/license/backup/admin keep admin-only via existing `require_role`.
  - Token creation UI: locate existing API-token component (`grep -r "api_tokens" frontend/src`); add a scope multi-select or preset buttons (agent-read / agent-write / admin).
  - **Acceptance:** contract test `test_api_token_scopes.py` — agent:read token rejected on a POST write endpoint; agent:write accepted; admin accepted on an admin endpoint; agent:write rejected on an admin endpoint.

- [ ] **T1.5 — Error envelope**
  - New `backend/core/errors.py` — `OdinError(code: str, detail: str, retriable: bool, status: int)` + enum of stable codes (e.g. `printer_not_found`, `quota_exceeded`, `idempotency_conflict`, `dry_run_unsupported`, `scope_denied`, etc.).
  - Global exception handler in `backend/app/factory.py` translates `OdinError` + `HTTPException` into `{"error": {"code", "detail", "retriable"}}`.
  - Migrate existing routes: any `raise HTTPException(status_code=404, detail="Printer not found")` becomes `raise OdinError("printer_not_found", "Printer N not found", retriable=False, status=404)`. Do this for the agent-surface endpoints only in this track (not the whole 392-endpoint codebase).
  - Add a `make lint-errors` check that greps for hand-rolled error dicts on agent endpoints.
  - **Acceptance:** contract test `test_error_envelope.py` — hit a known-missing printer, assert envelope shape.

- [ ] **T1.6 — `next_actions` hint on write responses**
  - Convention: every write route on the agent surface returns `next_actions: list[{tool, args, reason}]` in the body.
  - Define a small helper `build_next_actions(...)` in `backend/core/responses.py` to keep it terse and consistent.
  - Wire on every write endpoint listed in Phase 2's tool surface.
  - **Acceptance:** contract test `test_next_actions.py` — call POST /queue/add, assert response has `next_actions` with at least 2 entries pointing to `get_job` and `list_queue`.

- [ ] **T1.7 — `ODIN_ITAR_MODE=1` hard-lock**
  - Read env on boot in `backend/app/factory.py`. If `ITAR_MODE=1`:
    - Refuse boot if `license_server_url` is set and not on 127.0.0.1/RFC1918.
    - Refuse boot if any webhook URL in `system_config` is not on RFC1918.
    - Log clearly: `ITAR mode: outbound destination X violates air-gap. Fix config or unset ODIN_ITAR_MODE.`
  - Augment existing `safe_post`: when `ITAR_MODE=1`, reject destinations outside `127.0.0.1`/RFC1918 ranges with `OdinError("itar_outbound_blocked", ...)`.
  - **Acceptance:** contract test `test_itar_mode.py` — boot with ITAR=1 + localhost webhook succeeds; boot with ITAR=1 + public webhook refuses; `safe_post` to `8.8.8.8` blocked when ITAR=1, allowed when ITAR=0.

- [ ] **T1.8 — Phase 1 aggregate contract tests green**
  - Run full contract suite. Confirm no regressions against v1.8.8 baseline.
  - **Acceptance:** `make test` green; existing test count + Phase 1 new tests (roughly +30).

## Phase 2 — odin-mcp v2.0.0 — Live Operator Tool Surface

**Dependency:** Phase 1 must expose real endpoints (at minimum T1.1–T1.6 done).

- [ ] **T2.1 — odin-mcp client scaffolding**
  - New `src/client.ts`: reads `ODIN_BASE_URL`, `ODIN_API_KEY` from env. `fetch` wrapper with 5s default timeout, no retries.
  - Per-request `Idempotency-Key` generator (crypto.randomUUID) for write calls.
  - Passes `Authorization: Bearer ${api_key}` + optional `X-Dry-Run: true` based on `dry_run?: boolean` tool input.
  - Surface server error envelope through to MCP error result.
  - **Acceptance:** unit test `client.test.ts` passes — happy path + 404 handling + idempotency-key injection.

- [ ] **T2.2 — 11 read tools**
  - `list_printers`, `get_printer`, `list_jobs`, `get_job`, `list_queue`, `list_alerts`, `list_spools`, `list_filaments`, `list_maintenance_tasks`, `list_orders`, `farm_summary`.
  - Each: Zod input schema + Zod output schema. Output schemas validated before return.
  - All registered under `server.registerTool(name, {schema, handler})`.
  - **Acceptance:** all 11 tools present in MCP `list_tools` response with non-empty schemas.

- [ ] **T2.3 — 11 write tools**
  - `queue_job`, `cancel_job`, `approve_job`, `reject_job`, `pause_printer`, `resume_printer`, `mark_alert_read`, `dismiss_alert`, `assign_spool`, `consume_spool`, `complete_maintenance`.
  - Each write tool: input schema has optional `dry_run: z.boolean().default(false)` and optional `idempotency_key` (auto-generated if omitted).
  - Response includes `next_actions` passthrough.
  - **Acceptance:** all 11 write tools present + schema includes dry_run.

- [ ] **T2.4 — Reference.* namespace for v1 calculator tools**
  - Rename `calculate_print_cost` → `reference.calculate_print_cost`, `compare_farm_software` → `reference.compare_farm_software`, `recommend_printer_for_farm` → `reference.recommend_printer_for_farm`, `estimate_farm_capacity` → `reference.estimate_farm_capacity`.
  - These tools do not require `ODIN_BASE_URL`/`ODIN_API_KEY` (standalone).
  - **Acceptance:** `reference.*` tools work without env vars; non-`reference.*` tools error cleanly if env is unset.

- [ ] **T2.5 — Mock ODIN HTTP server for tests**
  - `test/mock-server.ts`: small node http server, configurable route-table, captures requests, can be driven by tests.
  - Supports assertion helpers: `assertCalledWith(method, path, body?)`, `assertCalledOnce(path)`, etc.
  - **Acceptance:** mock server used by T2.6 tests.

- [ ] **T2.6 — Integration test suite**
  - `test/integration.test.ts` covers every tool at minimum:
    - Happy path (mock returns 200, tool output schema validates).
    - Error path (mock returns 404/409, tool surfaces proper MCP error).
  - Cross-cutting tests:
    - Idempotency: call `queue_job` twice with same idempotency_key arg, mock sees 1 "real" request + 1 "replay".
    - Dry-run: call `queue_job` with `dry_run: true`, mock receives `X-Dry-Run: true` header, response has `dry_run: true`.
    - Schema rejection: call with bad input, asserted to fail client-side before HTTP.
  - **Acceptance:** `npm test` green; ≥ 40 assertions.

- [ ] **T2.7 — README v2 update**
  - Two sections: "Live tools (requires ODIN instance)" and "Reference tools (no ODIN required)".
  - Env var docs. Install snippet. Example prompts.
  - Migration note from v1 → v2 (breaking: reference tools renamed; new tools added).
  - **Acceptance:** `cat README.md` reads clean; markdown lint pass.

- [ ] **T2.8 — Build + publish-dry-run**
  - `npm run build` — dist/ clean.
  - `npm version 2.0.0` — staged, not pushed.
  - `npm publish --dry-run` — confirms package contents.
  - Actual publish happens in T6.4.
  - **Acceptance:** dry-run output includes all expected files, version bumped.

## Phase 3 — OpenClaw Skill Package

**Dependency:** Phase 2 done (skill delegates to odin-mcp).

- [ ] **T3.1 — `odin-skill/` directory**
  - In odin-mcp repo: `odin-skill/SKILL.md`, `odin-skill/skill.yaml`.
  - Describes tools, env vars, install snippet (local path + future ClawHub).
  - **Acceptance:** `skill.yaml` validates against OpenClaw's skill schema (use the published schema URL or inline copy).

- [ ] **T3.2 — Skill entry-point wrapper**
  - `odin-skill/bin/odin-skill` (POSIX shell): execs `node /path/to/odin-mcp/dist/index.js` with env passthrough.
  - No code duplication; skill is thin shim over MCP stdio.
  - **Acceptance:** `./odin-skill/bin/odin-skill` launches MCP server, prints tool list.

- [ ] **T3.3 — Local OpenClaw integration test**
  - Install OpenClaw on workstation (`brew install openclaw` or pip, whichever is supported).
  - Register the skill locally: `openclaw skills add ./odin-skill`.
  - Against a locally-running ODIN v1.8.9 + valid agent-write API key, run a prompt like `"list my printers"` and confirm the agent invokes `list_printers` successfully, returns a formatted answer.
  - Capture the transcript to `odin-skill/examples/session-01.md`.
  - **Acceptance:** transcript shows correct tool call + response.

- [ ] **T3.4 — PR to awesome-openclaw-skills**
  - Fork `VoltAgent/awesome-openclaw-skills`, add an entry under the 3D-printing / farm-management category.
  - Open PR. Capture URL in `odin-skill/REGISTRY_PR.md`.
  - **Acceptance:** PR URL captured; merge not required for track close.

## Phase 4 — CMMC-Clean Local-LLM Reference Stack

**Dependency:** Phases 1, 2, 3 done. Phase 4 runs the full stack.

- [ ] **T4.1 — `deploy/agent-runtime/docker-compose.yaml`**
  - Services: `ollama` (official image, model pre-pull optional), `odin` (GHCR image), `odin-mcp` (node:20-alpine, mounts odin-mcp dist), `openclaw` (official image if present; else stub an entry with TODO and a doc pointer — but we said no stubs: if no official image exists, use the locally-registered skill from T3.3 directly via the `odin-mcp` container).
  - Network: `nooutbound` bridge, plus `init` container that runs `iptables -A OUTPUT -o eth0 -d ! 127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16 -j DROP` inside the namespace (or equivalent via `cap_add: NET_ADMIN`).
  - Healthchecks on each service.
  - **Acceptance:** `docker compose up -d` brings all services healthy; a manual `curl ollama:11434` succeeds; `curl 8.8.8.8` from inside the network is blocked.

- [ ] **T4.2 — Reference stack docs + 3 prompts**
  - `deploy/agent-runtime/README.md`: hardware floor, model choices (32B primary, 14B/Q5 fallback, 7B not-recommended), tool-call reliability table (filled in after T4.5).
  - `deploy/agent-runtime/prompts/farm-operator.txt`, `approval-desk.txt`, `inventory-watchdog.txt` — system prompts targeted at each use case.
  - **Acceptance:** README renders; prompts pass a lint (no obvious injection vulnerabilities like raw tool output embedding).

- [ ] **T4.3 — `smoke.py` harness**
  - `deploy/agent-runtime/smoke.py` drives Ollama via HTTP (not `ollama run` subprocess — reproducible).
  - Loops through the 10-step test plan from spec R11.
  - Asserts tool call happened for each step. Captures full transcript to `artifacts/agent-smoke-<timestamp>.jsonl`.
  - Exit 0 on all-pass, 1 on any fail.
  - **Acceptance:** `python3 smoke.py --help` prints; dry-mock run passes with a stubbed Ollama.

- [ ] **T4.4 — `make agent-smoke` target**
  - Root `Makefile` in odin repo: `agent-smoke:` target composes up the stack (if not already), seeds minimal fixture data (one printer, one model, one spool), runs `smoke.py`, captures artifact.
  - **Acceptance:** `make agent-smoke` from clean checkout completes end-to-end.

- [ ] **T4.5 — ACTUAL smoke run against Qwen2.5-32B**
  - On this workstation (M4, 64GB unified memory), pull `qwen2.5:32b-instruct` in Ollama.
  - Run `make agent-smoke`. Capture real pass/fail per step.
  - If <8/10 steps pass: iterate. Investigate failures: bad prompt, ambiguous tool schema, missing next_actions hint, model quirk. Fix schema / prompts / hints (NOT stubbing out failing steps).
  - If 32B cannot be made reliable: document actual failure modes + fall back to Qwen2.5-72B (larger) or Llama 3.3-70B. Re-run smoke with the new model. Do NOT ship without a passing smoke.
  - **Acceptance:** `artifacts/agent-smoke-final.jsonl` shows ≥ 10/10 pass on whichever model is documented as the primary recommendation.

- [ ] **T4.6 — Tool-call reliability table filled in**
  - Update `deploy/agent-runtime/README.md` with a real measured table: model × tool → pass rate, derived from T4.5 transcripts (minimum 3 runs per model).
  - Note any specific failure modes observed and the mitigation.
  - **Acceptance:** table in README is populated with real numbers, not placeholders.

## Phase 5 — Site + Release Prep

**Dependency:** Phases 1–4 done. Parallel with Phase 6 final release.

- [ ] **T5.1 — `/agents` page on odin-site**
  - New page: `src/pages/agents.tsx` (check actual file tree — may be `app/` or `pages/` dir).
  - Hero + 3 cards (MCP / OpenClaw / CMMC Recipe) + install snippets + GitHub links.
  - Copy: CMMC/ITAR focus, "your model, your perimeter, your data."
  - Nav entry added (header + footer if applicable).
  - **Acceptance:** `npm run build` clean; page renders in local dev; Lighthouse score ≥ 90.

- [ ] **T5.2 — CHANGELOG entries (3 repos)**
  - `odin/CHANGELOG.md`: v1.8.9 entry — backend primitives, ITAR mode, agent-surface endpoints.
  - `odin-mcp/CHANGELOG.md`: v2.0.0 entry — breaking change, live tools.
  - `odin-site/CHANGELOG.md`: new `/agents` page.
  - Cross-reference each other.
  - **Acceptance:** all three CHANGELOG entries exist with coherent release notes.

- [ ] **T5.3 — README cross-links**
  - `odin/README.md` gets a "Agents" section linking to `/agents`, odin-mcp, and the CMMC recipe.
  - `odin-mcp/README.md` links back to ODIN + the OpenClaw Skill registry entry.
  - **Acceptance:** link check green.

## Phase 6 — CI + Release

**Dependency:** Phase 5 done.

- [ ] **T6.1 — MCP integration CI (odin-mcp)**
  - `.github/workflows/mcp-integration.yml` — runs on push + nightly.
  - Steps: checkout, node setup, `npm ci`, `npm run build`, `npm test`.
  - Runs on self-hosted runner (per project feedback rule).
  - **Acceptance:** workflow file lints; first run green.

- [ ] **T6.2 — Agent-smoke nightly (odin)**
  - `.github/workflows/agent-smoke.yml` — nightly at 05:00 UTC.
  - M4 self-hosted runner (GPU access).
  - Runs `deploy/agent-runtime/docker-compose.yaml up -d`, waits for health, `make agent-smoke`, captures artifacts, teardown.
  - On failure: dispatches an alert via the existing nightly-alert webhook (no new infra).
  - **Acceptance:** workflow file lints; first run green on M4.

- [ ] **T6.3 — Cut v1.8.9 (odin)**
  - `make release VERSION=1.8.9`.
  - GHCR image tagged + latest pushed.
  - **Acceptance:** GHCR shows `1.8.9` tag.

- [ ] **T6.4 — Publish odin-mcp v2.0.0 (npm)**
  - `npm publish`.
  - Verify listed on npmjs.com.
  - **Acceptance:** `npm view odin-print-farm-mcp version` returns `2.0.0`.

- [ ] **T6.5 — odin-site sync + promote**
  - `chore(sync): bump to v1.8.9` commit on odin-site main.
  - Vercel auto-promote workflow fires (validated earlier today).
  - **Acceptance:** runsodin.com/agents returns 200 with expected content.

- [ ] **T6.6 — End-to-end verification**
  - Hit `runsodin.com/agents` — page renders.
  - Install `odin-print-farm-mcp@2` globally — binary runs.
  - Clone odin v1.8.9, `make agent-smoke` end-to-end on M4.
  - **Acceptance:** manual smoke checklist green; all three repos aligned.

## DAG

```
Phase 1 — Backend (foundation, sequential for T1.1–T1.2 → T1.3/T1.4/T1.5 parallel → T1.6 → T1.7 → T1.8)

  T1.1 (migration) ───────────────────────────────────┐
    │                                                  │
    ├─► T1.2 (idempotency) ──┐                        │
    │                         │                        │
    ├─► T1.3 (dry-run wire) ──┼─► T1.5 (errors) ──► T1.6 (next_actions) ──┐
    │                         │    (sequenced to avoid route-file races)   │
    └─► T1.4 (scopes) ────────┘                                            │
                                                                            │
                T1.7 (ITAR) ─ independent ─────────────────────────────────┤
                                                                            │
                                                     T1.8 (aggregate) ◄────┘

Phase 2 — MCP (depends on Phase 1 endpoints landing)

  T2.1 (client) ──► T2.2 (reads) ──┐
                                    ├─► T2.6 (tests) ──► T2.7 (README) ──► T2.8 (build)
                    T2.3 (writes) ──┤
                                    │
                    T2.4 (reference)┤
                                    │
                    T2.5 (mock srv)─┘

Phase 3 — Skill (depends on Phase 2)

  T3.1 (dir) ──► T3.2 (wrapper) ──► T3.3 (local test) ──► T3.4 (registry PR)

Phase 4 — CMMC recipe (depends on Phases 1,2,3)

  T4.1 (compose) ──┐
                    ├─► T4.3 (smoke.py) ──► T4.4 (make target) ──► T4.5 (REAL smoke) ──► T4.6 (table)
  T4.2 (docs) ─────┘

Phase 5 — Site (depends on Phase 4 for accurate docs)

  T5.1 (/agents) ──► T5.2 (CHANGELOG) ──► T5.3 (cross-links)

Phase 6 — Release (depends on Phase 5)

  T6.1 (mcp CI) ──┐
                  ├─► T6.3 (v1.8.9) ──► T6.4 (mcp npm) ──► T6.5 (site sync) ──► T6.6 (verify)
  T6.2 (smoke CI)─┘
```

Parallelism opportunities:
- Phase 1 middle tier (T1.3, T1.4, T1.5) can run parallel.
- Phase 2 reads/writes/reference/mock can run parallel once T2.1 exists.
- T1.7 (ITAR) is independent of the rest of Phase 1.
- Phase 4 T4.1 + T4.2 parallel.

## Post-Loop Steps

Per spec + metadata, after evaluate-loop PASS:

1. **Codex adversarial review** on the full track diff.
2. Fix findings (each fix cycle = one executor re-run + re-evaluation).
3. Loop until codex returns clean.
4. Then mark track `COMPLETE`.

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| Qwen2.5-32B fails tool-call reliability on ODIN surface | T4.5 explicitly allows falling back to 72B/70B. Schema tightening + next_actions hints are the first lever. |
| OpenClaw skill spec diverges from what I've inferred | T3.1 validates against published schema; T3.3 actually registers + runs. If schema shape is wrong, iteration is contained to one task. |
| Migration 005 conflicts with pending v1.8.8 migration 004 | T1.1 contract test covers upgrade from v1.8.8 explicitly. |
| Dry-run per-route blows up test count | Dry-run is opt-in per route; we wire a focused set (queue, printer, spool, maintenance, alert, order notes) — not all 392. |
| M4 agent-smoke nightly flaps on GHA | Use existing nightly-alert webhook; dedupe within 1h. No new alerting infra. |
| ITAR-mode env var leaks through to unrelated test paths | Explicit `@pytest.fixture(autouse=False)` — must be requested per test; other tests default ITAR=0. |

## Definition of Done (Track-Level)

1. All Phase 1–6 checkboxes ticked with acceptance verified.
2. Evaluate-loop PASS from loop-execution-evaluator.
3. Codex adversarial review PASS (zero open findings from the review).
4. `tracks.md` updated — track moved to "Completed Tracks" with summary.
5. `metadata.json` — `current_step = "COMPLETE"`, `step_status = "PASS"`.

## Plan Evaluation Report

| Check | Status | Notes |
|-------|--------|-------|
| Scope Alignment | PASS | All 17 spec requirements mapped to tasks. ITAR (R12) relocated spec Phase 4 → plan Phase 1 (backend boot path). |
| Overlap Detection | PASS | Verified against completed tracks. Reuses `safe_post`, API key model, scheduler, audit writer; no duplication. |
| Dependencies | PASS | Topological order verified. T1.5 sequenced after T1.3 to avoid route-file race. No cycles. |
| Task Quality | PASS | 34/34 tasks have paths + acceptance. Two minor resolve-at-exec items (T1.4 admin UI filename, T4.1 OpenClaw image conditional) both have escape hatches documented. |
| DAG Valid | PASS | All IDs unique. Parallel groups verified for file-level non-overlap after T1.5 re-sequencing. |
| Board Review | DEFERRED_TO_CODEX | User directive: post-loop codex adversarial review is the explicit external-review gate. Stronger than board review for this scope. |

### Verdict: PASS

Ready for executor.
