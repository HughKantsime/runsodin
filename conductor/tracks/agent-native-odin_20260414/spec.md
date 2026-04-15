# Agent-Native ODIN — OpenClaw + MCP Integration

**Track ID:** `agent-native-odin_20260414`
**Created:** 2026-04-14
**Type:** feature + integration
**Target release:** ODIN v1.8.9, odin-mcp v2.0.0
**Repos touched:** `odin`, `odin-mcp`, `odin-site`

## Goal

Make ODIN a first-class citizen in LLM-agent workflows — OpenClaw
Skills + MCP — **without touching the air-gapped / CMMC deployment
story**. The agent runs outside ODIN's perimeter, calls in via the
same auth as a human, and runs on an LLM of the operator's choice
(local Ollama for CMMC/ITAR, cloud model for everyone else).

Three overlapping goals, one track:

1. **Replace the odin-mcp reference-calculator with a live operator
   tool-surface.** Current `odin-mcp` is standalone math (cost,
   capacity, comparison). That's a brochure. v2 talks to a running
   ODIN instance, authenticated, and drives the core farm-operator
   loop: printers, queue, jobs, inventory, maintenance, alerts.

2. **Harden the ODIN backend for agent traffic.** Idempotency keys,
   dry-run mode, structured error envelopes, agent-scoped API tokens.
   These are the primitives that make a 32B local model competent
   enough to drive a farm without hallucinating retries into duplicate
   work.

3. **Ship the CMMC-clean agent runtime recipe.** Documented,
   reproducible, tested end-to-end: Ollama + Qwen2.5-32B-Instruct +
   odin-mcp + ODIN, all self-hosted, zero outbound calls. And an
   OpenClaw Skill package so operators who already run OpenClaw can
   drop ODIN into their existing setup.

Close everything we can in one release. Anything that requires
hardware we don't have (a printer on this workstation) or a product
decision we can't make autonomously gets **explicitly listed as
Out of Scope** — not "filed as follow-up." We do not stub. We do not
defer.

## Why

User research (2026-04-14): r/openclaw is 103k members and actively
using OpenClaw Skills to drive 3D-printing workflows. `openclaw/bambu-
cli` is a live Skill that covers single-printer ops (MQTT/HTTPS/FTPS,
start/pause/AMS/camera). `3DQue`/`AutoFarm3D`/`FlowQ` are farm-level
but not agent-native. **Nobody is shipping a farm-orchestration Skill.**
Academic validation exists (LLM-3D Print, arXiv 2408.14307) — LLM
agents catching extrusion/warping/adhesion issues, 5× peak load
improvement on optimized prints.

Secondary: the shops most likely to adopt agent-driven farm operation
are also the shops under CMMC / ITAR — defense contractors, aerospace
subs — who already run local LLMs and can't send print metadata to a
cloud model. The ITAR story and the agent story turn out to be the
same story if we build it right.

## Requirements

### Phase 1 — ODIN backend agent primitives

**R1 — Idempotency-Key middleware.**

New FastAPI middleware applied to all mutating routes (`POST`,
`PUT`, `PATCH`, `DELETE`). If the client sends `Idempotency-Key:
<uuid>`, the middleware:

- Computes `request_hash = sha256(method + path + body)`.
- Looks up `idempotency_keys(key, user_id)` — if present and hash
  matches, returns the cached response with `X-Idempotent-Replay:
  true` header. Status code preserved.
- If present but hash differs: 409 Conflict with clear error message
  ("idempotency key already used with a different request").
- If absent: proceeds, captures response status + body, writes to the
  table keyed on `(key, user_id)`. 24-hour TTL.

New migration `005_add_idempotency_keys_and_api_key_scopes.sql`:
```sql
CREATE TABLE IF NOT EXISTS idempotency_keys (
  key TEXT NOT NULL,
  user_id INTEGER NOT NULL,
  method TEXT NOT NULL,
  path TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  response_status INTEGER NOT NULL,
  response_body TEXT NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (key, user_id)
);
CREATE INDEX IF NOT EXISTS ix_idempotency_keys_created_at
  ON idempotency_keys(created_at);
```

Background task in the existing scheduler prunes rows older than 24h
hourly.

**R2 — `X-Dry-Run: true` header support on mutating routes.**

For any route with `Idempotency-Key` middleware, if the request also
has `X-Dry-Run: true`, the route:

- Runs all validation (auth, RBAC, schema, business rules).
- Returns a 200 response with `{"dry_run": true, "would_execute":
  {...what-would-change...}}` body.
- **Does not commit to DB.** Implementation: new `request.state.dry_run`
  flag; route handlers check it at commit-point; each route is
  responsible for either building the preview dict or short-circuiting
  before any side-effect. We do this **per-route, explicitly**, not via
  global transaction-rollback — a naive rollback can leak half-done
  state via cross-module events, background workers, or external-call
  side effects (webhook dispatch, filesystem writes).

Applies to: queue mutations, printer control, spool ops, maintenance,
alert ops, order notes, revision ops. **Does not apply** to auth
endpoints, license activation, backup ops, user CRUD, admin settings —
those stay humans-only.

**R3 — Agent-scoped API tokens.**

Extend existing API token system (`core.security.api_keys`):

- New enum field `scope: Literal["admin", "agent-write", "agent-read"]`.
- `agent-read` = read-only access to the agent tool surface (list_*,
  get_* tools). Denied on any write endpoint.
- `agent-write` = read + write access to the *agent-allowed* surface
  (printers, queue, jobs, inventory, maintenance, alerts, orders).
  Denied on auth/license/backup/user/admin endpoints.
- `admin` = current behavior.

New `require_scope(*allowed_scopes)` dependency that checks the
token's `scope` field against an allow-list.

Token creation UI (admin settings): dropdown to pick scope, shows
summary of what that scope grants in plain English.

Migration 005 also adds:
```sql
ALTER TABLE api_keys ADD COLUMN scope TEXT NOT NULL DEFAULT 'admin';
```

**R4 — Standardized error envelope.**

All error responses (4xx and 5xx) return:
```json
{
  "error": {
    "code": "printer_not_found" | "quota_exceeded" | ...,
    "detail": "human-readable",
    "retriable": false
  }
}
```

Implementation: single FastAPI exception handler for `HTTPException`
and custom `OdinError` that produces the envelope. Existing routes
that hand-roll error dicts are migrated. Machine-readable `code` field
is a new stable contract — enumerated in `core/errors.py`.

**R5 — `next_actions` hint on write responses.**

Every agent-surface write endpoint returns a `next_actions: list[dict]`
field in the 200/201 response body, suggesting the next reasonable
tool call. Example: `POST /api/v1/queue/add` returns
```json
{
  "job_id": 42,
  "status": "queued",
  "next_actions": [
    {"tool": "get_job", "args": {"id": 42}, "reason": "check status"},
    {"tool": "list_queue", "args": {}, "reason": "see queue depth"}
  ]
}
```

Pure hint — no enforcement. Designed to make weak local models
competent without prompt-engineering heroics.

### Phase 2 — odin-mcp v2.0.0 — live operator tool surface

**R6 — Rewrite odin-mcp as a live ODIN client.**

Current `odin-mcp` is standalone JSON data + math. v2 is a live client:

- Reads `ODIN_BASE_URL` and `ODIN_API_KEY` from env.
- Uses `fetch` with bounded timeouts (5s default, configurable per
  tool), no retries (idempotency key is the retry primitive).
- Every write tool generates a UUID `Idempotency-Key` and passes it.
- Every tool has bounded Zod schema on input AND output.
- `@modelcontextprotocol/sdk` latest version; stdio transport only
  (no HTTP — CMMC-friendly).

**Tool surface (v2):**

Read-only:
- `list_printers(status_filter?)`
- `get_printer(id)` — full status + active job + AMS
- `list_jobs(status_filter?, limit?)`
- `get_job(id)`
- `list_queue()`
- `list_alerts(severity?, unread_only?)`
- `list_spools(filament_filter?, available_only?)`
- `list_filaments()`
- `list_maintenance_tasks(overdue_only?)`
- `list_orders(status_filter?)`
- `farm_summary()` — one-shot dashboard

Write (require idempotency key, honor `dry_run`):
- `queue_job(model_id, printer_id?, quantity?)`
- `cancel_job(job_id, reason)`
- `approve_job(job_id, notes?)`
- `reject_job(job_id, reason)`
- `pause_printer(printer_id)`
- `resume_printer(printer_id)`
- `mark_alert_read(alert_id)`
- `dismiss_alert(alert_id)`
- `assign_spool(spool_id, printer_id, ams_slot?)`
- `consume_spool(spool_id, grams)`
- `complete_maintenance(task_id, notes?)`

**Not exposed to agents:** user CRUD, license activation, backup,
admin settings, SMTP config, RBAC edits. Humans only.

Reference-calculator tools from v1 (`calculate_print_cost`,
`compare_farm_software`, `recommend_printer_for_farm`,
`estimate_farm_capacity`) are **kept** — they have standalone value
for pre-sales research. Stay under separate `reference.*` namespace.

**R7 — MCP tool integration tests.**

New test suite `odin-mcp/test/integration.test.ts`:

- Spins up a mock ODIN HTTP server (node `http` module, no ODIN
  required).
- For each tool: happy path + error path (mock returns 4xx).
- Idempotency: calls write tool twice with same key, asserts second
  response has `X-Idempotent-Replay: true`.
- Dry-run: calls write tool with `dry_run: true`, asserts response
  has `dry_run: true` and mock never saw a "real" call beyond the
  validation request.
- Schema validation: malformed input rejected client-side before
  HTTP.

Run via `npm test`. Wired into odin-mcp CI.

### Phase 3 — OpenClaw Skill package

**R8 — `odin-skill/` directory in odin-mcp repo.**

Per OpenClaw skill spec:
- `SKILL.md` — human-readable description, example prompts, install
  instructions.
- `skill.yaml` — machine manifest: name, version, description, entry
  point, required env vars, tools listing.
- Entry point is a thin wrapper that shells out to `odin-mcp` via
  stdio MCP protocol (same binary, no code duplication).

Test locally: install OpenClaw on the workstation, register the
skill, send a prompt like "show me my printers" — agent calls
`list_printers`, response rendered.

**R9 — PR to `VoltAgent/awesome-openclaw-skills` registry.**

After local end-to-end works, open a PR adding ODIN to the registry.
Repo link, one-line summary, category (tool: 3D-printing / farm
management).

This one might take review cycles on the upstream side — out of our
control. **Definition of done:** PR opened, branch pushed, link
captured. Merge is not required for this track to close.

### Phase 4 — CMMC-clean local-LLM reference stack

**R10 — `deploy/agent-runtime/` reference stack.**

New directory in odin repo:
- `docker-compose.yaml` — Ollama + `qwen2.5:32b-instruct` (pulled
  lazily on first run) + odin-mcp + ODIN backend. All services on a
  `nooutbound` bridge network with a `deny-outbound` iptables rule
  installed by an init container. Documented as such.
- `README.md` — hardware requirements (~24GB VRAM or 64GB unified
  memory for 32B Q4; fallback to Qwen2.5-14B or -7B for smaller
  boxes), pull commands, smoke script.
- `prompts/` — 3 tested system prompts:
  - `farm-operator.txt` — day-to-day ops agent.
  - `approval-desk.txt` — agent that triages new job submissions.
  - `inventory-watchdog.txt` — agent that watches spool levels.

**R11 — `make agent-smoke` end-to-end test.**

Scripted multi-turn test plan (10 steps):
1. `list_printers` → expect array with at least one printer in
   seeded fixture
2. `list_queue` → expect empty or seed-populated queue
3. `queue_job(model_id=1, printer_id=1)` with dry_run → expect
   preview
4. `queue_job(model_id=1, printer_id=1)` for real → expect job_id
5. `get_job(job_id)` → expect queued status
6. `cancel_job(job_id, reason="smoke")` → expect cancelled
7. `list_alerts()` → expect array
8. `list_spools(available_only=true)` → expect array
9. `farm_summary()` → expect all keys present
10. Retry step 4 with same idempotency key → expect cached response

Driven by a small Python harness (`deploy/agent-runtime/smoke.py`)
that shells out to `ollama run` with a structured prompt. Exit code
0 on all-pass, 1 on any fail. Captures tool-call transcript to
`artifacts/agent-smoke.jsonl` for postmortem.

Ships the Qwen2.5-32B recipe as the **documented** stack. If 32B
turns out to be unreliable in the smoke, document the actual failure
modes + fall back to 70B (Llama 3.3) or Claude (for non-CMMC
operators) in the README — but actually run the smoke and report real
numbers, don't hand-wave it.

**R12 — ITAR-mode container-level egress lock.**

New env var `ODIN_ITAR_MODE=1`:
- Container refuses to start if ODIN's own outbound allow-list is
  non-empty (e.g. license_server_url set, webhook URLs configured).
- HTTP client layer (existing `safe_post`) adds an extra guard: if
  `ITAR_MODE=1` and destination is not 127.0.0.1 / LAN RFC1918 ranges,
  refuse outright (instead of just SSRF-safe validation).
- Test: integration test spins ODIN with `ODIN_ITAR_MODE=1`, confirms
  boot ok, confirms webhook dispatch to `8.8.8.8` is refused.

This is the CMMC hardening story in one flag.

### Phase 5 — Site + distribution

**R13 — `/agents` page on odin-site.**

New page at `runsodin.com/agents`:
- Hero: "ODIN speaks agent. OpenClaw-ready. MCP-ready. Air-gap-safe."
- Three cards: MCP (odin-mcp v2), OpenClaw Skill, CMMC Recipe.
- Install snippet for each.
- Links to GitHub + docs.

Copy focus: CMMC/ITAR shops with local LLM. "Your model, your
perimeter, your data." Avoid generic AI-hype copy.

**R14 — CHANGELOG + release coordination.**

- ODIN `v1.8.9` — backend primitives (R1–R5, R12).
- odin-mcp `v2.0.0` — breaking change, new tool surface (R6–R8).
- odin-site — `/agents` page (R13).
- odin-mcp README updated to show both v1 reference tools AND v2 live
  tools.
- Cross-reference each other in CHANGELOGs.

### Phase 6 — CI + release

**R15 — MCP integration CI.**

New workflow `.github/workflows/mcp-integration.yml` in odin-mcp repo:
- Runs on push + nightly.
- Spins up a mock ODIN backend (node script in `test/`).
- Runs `npm test`.
- Reports pass/fail.

**R16 — Agent-smoke nightly.**

New workflow `.github/workflows/agent-smoke.yml` in odin repo (M4
self-hosted runner):
- Nightly.
- Runs `deploy/agent-runtime/docker-compose.yaml up -d` →
  `make agent-smoke` → teardown.
- Alerts on failure via the existing alert-webhook that's used for
  nightly tests. **No new alerting infra.**
- Because the M4 runner is the only thing with the GPU, this is
  self-hosted only. Matches existing feedback rule.

**R17 — Cut the releases.**

- `make release VERSION=1.8.9` in odin
- `npm version 2.0.0 && npm publish` in odin-mcp
- odin-site chore(sync) + Vercel auto-promote
- Verify all three live

## Acceptance Criteria

**Backend primitives (Phase 1):**
- [ ] Migration 005 applies cleanly on a fresh DB and an upgraded DB.
- [ ] Idempotency middleware: second POST with same key returns
      cached response + `X-Idempotent-Replay: true`. Different body
      returns 409.
- [ ] Dry-run middleware: `X-Dry-Run: true` on `POST /queue/add`
      returns `{"dry_run":true, "would_execute":{...}}` and no DB row
      is created. Integration test pins this.
- [ ] `api_keys.scope` column exists and `require_scope(...)` blocks
      agent-read tokens from calling write endpoints. Contract test.
- [ ] All error responses include the `error.code`/`error.retriable`
      envelope. Lint-level check that `raise HTTPException` without
      an `OdinError` is caught. Enumerated codes in `core/errors.py`.
- [ ] Every agent-surface write endpoint returns `next_actions` in
      the 200 body.

**odin-mcp v2 (Phase 2):**
- [ ] `npm test` passes in odin-mcp with integration tests green.
- [ ] 11 read tools + 11 write tools registered; schema validated.
- [ ] Idempotency + dry-run tests pass against the mock server.
- [ ] README v2 updated: reference tools + live tools sections.

**OpenClaw Skill (Phase 3):**
- [ ] `odin-skill/` directory exists with SKILL.md + skill.yaml.
- [ ] Local `openclaw` installation registers the skill successfully
      and a sample prompt drives `list_printers` against a live local
      ODIN.
- [ ] PR opened against `VoltAgent/awesome-openclaw-skills` (merge
      not required).

**CMMC recipe (Phase 4):**
- [ ] `docker compose up` in `deploy/agent-runtime/` brings up
      Ollama + Qwen2.5-32B + odin-mcp + ODIN on the M4 box.
- [ ] `make agent-smoke` runs the 10-step plan against the stack,
      exits 0, artifacts captured.
- [ ] `ODIN_ITAR_MODE=1` refuses outbound to 8.8.8.8 in contract test
      but still passes the agent smoke (which is all localhost).

**Site + release (Phase 5–6):**
- [ ] `/agents` page live on runsodin.com.
- [ ] ODIN v1.8.9 image on GHCR.
- [ ] odin-mcp v2.0.0 on npm.
- [ ] Nightly agent-smoke workflow passes its first run.

## Out of Scope

- **Vision-based defect detection pipeline using LLMs.** Separate
  track when we have a printer on the bench to stream frames from.
  R10's CMMC recipe can be extended with this later; not this track.
- **Chat-platform front-ends (Telegram/Discord/iMessage as ODIN
  UIs).** Separate track. OpenClaw provides this via its own channel
  layer — ODIN doesn't need to own it.
- **`bambu-cli` subprocess delegation from inside ODIN tools.**
  Different security model (shelling out to third-party CLI per
  request). Left to a future track once we have a hardened subprocess
  harness.
- **Agent-initiated billing / payment / license operations.** Humans
  only, now and forever.
- **Multi-agent handoff / orchestration logic inside odin-mcp.**
  OpenClaw owns that layer. We expose tools, it coordinates agents.
- **Model-Zoo hosting for ODIN-flavored fine-tunes.** If it turns out
  Qwen2.5-32B is unreliable against our tool surface, the fix is
  schema tightening, not a fine-tune. Revisit only if schema work
  doesn't close the gap.
- **Automatic Ollama model pre-pulling at ODIN install time.**
  30-40GB download; operator decides when to do this. Documented but
  not automated.
- **Cloud-agent-as-a-service (e.g. "ODIN managed agent" endpoint).**
  Violates the ITAR / CMMC story. Not on the roadmap.

## Technical Notes

### Migration 005 combines two unrelated schema changes

Idempotency table + api_keys.scope column. Precedent: migration 004
combined setup-token delete + digest delivery-status in the
user-friction track. Same rationale. Keep them in one file for this
release.

### Idempotency key scope: per-user, not global

If two users happen to generate the same UUID (vanishing but
non-zero probability for UUIDv4), we should not hand user B the
response we sent user A. PK is `(key, user_id)` for this reason.
Also means the middleware looks up by both — index on `(key,
user_id)` is implicit from the PK.

### Dry-run surface: per-route, not global transaction-rollback

Tempting to wrap every mutating route in `db.begin() ... rollback`
when `X-Dry-Run: true`. Don't do it. Problems:
- Some routes emit async events (alert dispatch, webhook fan-out)
  that run in the background and don't participate in the outer
  transaction.
- Some routes write to the filesystem (upload endpoints).
- Some routes call external services (SMTP test, printer MQTT).

Per-route explicit handling forces each author to think about "what
changes, what doesn't" instead of relying on rollback magic.

### Agent-read vs agent-write scope split

agent-read tokens are the safer default for operators experimenting
with agents. "The bot can look at my farm but can't touch it" is the
baseline offer. Upgrade to agent-write only after testing.

Admin settings page gets a "Revoke" button on every token showing its
scope + last-used timestamp.

### OpenClaw skill registration nuances

OpenClaw skills can be loaded from a local path OR from ClawHub.
For this track, we ship the skill as a local-path install in the
docker-compose; the PR to `awesome-openclaw-skills` is "public
visibility" but not required for our own stack to work.

### CMMC recipe hardware floor

Qwen2.5-32B Q4 needs ~20GB VRAM for decent throughput. The M4 Mac
Mini has unified memory so this works on the dev box. Document a
fallback path for operators on smaller hardware:
- Q5 → ~24GB
- Q8 → ~34GB
- 14B Q4 → ~10GB (tool-calling reliability drops; verify against
  smoke before recommending)
- 7B Q4 → ~5GB (likely too weak; document as "not recommended")

### Don't stub the local-LLM smoke

The user directive is explicit: we do not stub, we do not defer. That
means R11 (`make agent-smoke`) must **actually run** against Ollama
on this workstation during the executor phase — not just commit a
script and call it done. If Qwen2.5-32B fails the smoke, the track
is NOT complete; we fix schema / prompt / model choice until it
passes or we replace it with a model that does.

### ITAR mode is a hard mode, not a soft one

`ODIN_ITAR_MODE=1` is not a "show extra warnings" mode. It's a
refuse-to-boot-if-misconfigured mode. If the operator set
`LICENSE_SERVER_URL` and then ITAR-mode-enabled, the container exits
1 with a clear message. They have to fix one or the other.

### Why kept the reference-calculator tools in odin-mcp v2

- Non-zero adoption signal on npm already.
- Zero overhead to leave them in under a `reference.*` namespace.
- Useful for *pre-sales* research (agent: "what printer should I buy
  for a farm that runs 24/7?") even when no live ODIN exists.

### Codex adversarial review requirement

Per user directive, after the evaluate-loop passes, run codex
adversarial review on the diff. Fix findings to green before marking
the track complete. Each fix cycle through codex counts as a normal
fix loop, not a new track.

## Success Looks Like

A defense-contractor print shop with a CMMC L2 perimeter:

1. Runs ODIN behind their firewall (existing — no change).
2. Drops the `deploy/agent-runtime/` compose file alongside.
3. Runs it. Local Qwen2.5-32B boots, odin-mcp registers, OpenClaw
   picks up the skill.
4. Their operator types "approve any jobs under 100g of PLA from
   engineering; reject the rest with 'out of spec'" into OpenClaw's
   Telegram bridge.
5. The agent calls `list_jobs(status_filter="pending")`,
   `get_job(id)` on each, then `approve_job` or `reject_job` with
   idempotency keys.
6. Zero outbound packets leave their network.
7. Every action lands in ODIN's existing audit log with an
   `agent:<token_name>` marker.

That's the demo. That's the positioning. That's the track.
