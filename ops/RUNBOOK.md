# O.D.I.N. Operator Runbook

This is the doc a person other than the original author uses to run
O.D.I.N. in production. It assumes no prior context about the system
and no shared conversations.

If you are shipping O.D.I.N. to a customer who will self-host, this is
the document they need on day one. Keep it honest — where the system
has rough edges, say so; do not paper them over.

---

## 0. TL;DR — 90-second orientation

O.D.I.N. is a single Docker container running FastAPI + supervisord-
managed background workers against a local SQLite database. It pulls
its image from the GitHub Container Registry and is restarted by
Watchtower when a new `:latest` is pushed. Data lives in a mounted
volume.

```
                  ┌──────────────────────┐
  GitHub repo ──► │  CI: Deploy workflow │ ──► ghcr.io/hughkantsime/odin:latest
                  └──────────────────────┘
                                              │
                           Watchtower polls   ▼
  ┌────────────┐    ┌────────────────────┐   pulls + restarts
  │ Your users │ ──►│ openresty / NPM    │ ────► Docker host
  │            │    │ (TLS + proxy)      │           │
  └────────────┘    └────────────────────┘           ▼
                                          ┌──────────────────┐
                                          │ container: odin  │
                                          │  ├ backend (8000)│
                                          │  ├ go2rtc        │
                                          │  └ monitors x N  │
                                          │  └ /data volume  │
                                          └──────────────────┘
                                                      │
                                          daily 03:00 │ vzdump
                                                      ▼
                                           ┌──────────────────┐
                                           │ NFS backup target│
                                           └──────────────────┘
```

**What you need before operating:**
- SSH access (or web console) to the Docker host
- Access to the GitHub repo (for deploys)
- Admin account on the ODIN instance (for token minting, user mgmt)
- An email inbox for failure alerts

---

## 1. Architecture — what component owns what

| Component | Where | Responsibility |
|---|---|---|
| FastAPI backend | container `odin`, port 8000 | HTTP API + HTML frontend + WebSocket |
| supervisord | PID 1 inside container | Starts & restarts backend + monitors |
| Printer monitors | background workers inside container | Bambu MQTT, Moonraker, PrusaLink, Elegoo poll loops |
| go2rtc | port 1984 (loopback) + 8555 (WebRTC) | Camera stream proxy |
| SQLite DB | `/data/odin.db` (mounted volume) | All application state |
| Encryption key | `/data/.encryption_key` | Fernet key for SMTP / MQTT passwords + camera URLs |
| JWT secret | `/data/.jwt_secret` | Signs bearer tokens from `/api/auth/login` |
| Uploaded models | `/data/uploads/` | 3MF / gcode files |
| Logs | `/data/backend.log` | Uvicorn + application stdout/stderr |

**Outside the container:**
- **Watchtower** — container on the same Docker host. Polls GHCR every ~5 min, pulls new `:latest`, restarts `odin`.
- **openresty / NPM** — reverse proxy on a different host (the LAN perimeter). Terminates TLS. Forwards to `odin:8000`.
- **Uptime Kuma** — watches the public URL, alerts via email on consecutive failures.
- **vzdump / Proxmox** — snapshots the LXC (or VM) holding the container daily to an NFS target.

---

## 2. Normal operations

### 2.1 Deploy a new version

```
# On your laptop:
git checkout main
git pull
# bump backend/VERSION (or run ops/bump-version.sh)
git commit -am "release: vX.Y.Z"
git push origin main
```

GitHub Actions:
1. **Validate Build** — frontend builds, backend deps install clean, design tokens in sync.
2. **Auto-Tag Release** — creates `vX.Y.Z` git tag from `VERSION` file.
3. **Build & Push Docker** — multi-arch image to `ghcr.io/hughkantsime/odin:latest` + `:vX.Y.Z`.
4. **Create GitHub Release** — tag + auto-generated notes.
5. **Sync odin-site** — triggers a downstream workflow in the marketing site repo.
6. **Verify Prod** — polls `/health` for 10 minutes waiting for the version to appear live. **Blocks the workflow if prod doesn't update.**
7. **Notify** — ntfy push with pass/fail status.

Expected end-to-end time: ~10 min from push to prod serving the new version.

### 2.2 Verify a deploy actually landed

```
curl -sS https://<your-odin-host>/api/v1/version
# → {"version": "X.Y.Z"}

curl -sS https://<your-odin-host>/health
# → {"status":"ok","version":"X.Y.Z","database":"...","spoolman_connected":false}
```

If these don't match your freshly-released version, Watchtower hasn't
reconciled yet (wait a few minutes) OR it's broken (see incident
response below).

### 2.3 Observe live traffic

From the Docker host:
```
docker logs --tail 200 odin
docker exec odin tail -f /data/backend.log
```

From anywhere (if SSH is unavailable), the container's health check runs every 30s; `docker ps` will show the health status.

---

## 3. Monitoring

### 3.1 Uptime Kuma

- **URL**: the Kuma instance behind `kuma.subsystem.app` (or equivalent).
- **Primary monitor**: watches `https://<your-odin-host>/health` or `/` — whichever is reachable.
- **Notification channel**: an SMTP-style channel (Resend / Mailgun / whatever is wired). Paging goes to the operator email.
- **Alert threshold**: typically 2 consecutive failures — that's ~10 min of downtime before an email fires.

**Silent-monitor trap**: a monitor can exist and record heartbeats WITHOUT being attached to a notification channel. If an outage looks silent, the first thing to check is that the monitor-to-notification link exists in Kuma's admin UI under the monitor's edit form. (Prod once went 19 hours unnoticed because this link was missing.)

### 3.2 Backups

- vzdump snapshots run daily at your configured hour (commonly 03:00 local).
- Retention: typically `keep-last: 3` (three days). **Consider bumping to 7+ for production** — three days is tight if a corruption goes undetected over a weekend.
- Target: NFS to a NAS. Confirm you can see recent files with:
  ```
  ls -lh /mnt/<nfs-target>/dump/vzdump-lxc-<ct-id>-*.tar.zst
  ```

**Alert-on-backup-failure**: Proxmox's `mailnotification=failure` route uses `sendmail` on the hypervisor. Unless an SMTP relay is configured there, failure mail may dead-letter locally. Send a deliberate failing backup occasionally to confirm emails actually deliver.

### 3.3 Backend log patterns worth watching

- `sqlite3.OperationalError: database is locked` — WAL contention. Transient under normal load; if frequent, review concurrent writer paths.
- `ItarOutboundBlocked` — ITAR mode refused a public-IP egress. Review the audited URL; legitimate outbound → add to allowlist or disable ITAR.
- `idempotency_in_progress` 409 — a client retried faster than the prior request completed. Client should back off.

---

## 4. Incident response

### 4.1 Diagnostic decision tree

```
Prod returning 5xx / timeout / nothing?
  │
  ├─ 502 Bad Gateway from the proxy
  │     → container is down or not listening on 8000
  │     → SSH to host; `docker ps` — is `odin` running?
  │         ├─ status "Restarting" → container crash loop → see 4.2
  │         ├─ status "Up" + unhealthy → backend stuck but process alive
  │         │     → `docker logs --tail 100 odin` + `docker exec odin tail /data/backend.log`
  │         └─ not listed → container removed/stopped; `docker start odin`
  │
  ├─ 500 from ODIN (`error.code` in body)
  │     → genuine application error; tail `/data/backend.log` for traceback
  │     → fix path depends on error code (see 4.3)
  │
  ├─ 401 Not authenticated / 403 scope_denied
  │     → expected when credentials are wrong or scope mismatched;
  │       not an incident unless it happens to working integrations
  │
  ├─ 504 Gateway Timeout
  │     → upstream `odin:8000` is slow — DB contention? printer monitor
  │       deadlock? Check `/data/backend.log` for slow queries
  │
  └─ Total unreachable (nothing answers on :80/:443)
        → check proxy host itself (openresty, NPM); ODIN is further downstream
```

### 4.2 Container crash-loop

**Most common cause**: failed migration on boot. The container prints each applied migration, and the traceback appears right after the last successful one.

```
docker logs --tail 80 odin 2>&1 | grep -E "Applied core|Traceback|Error"
```

If a SQL syntax error appears on a specific migration:
1. **Don't delete the migration file** — the DB state is partial and needs that migration.
2. **Patch the migration + core/db.py** (if the loader is the culprit) on a branch.
3. **Hot-patch the running container** while you build the fix:
   ```
   docker stop odin
   docker cp <local-fixed-file> odin:/app/backend/core/db.py
   docker cp <local-fixed-migration> odin:/app/backend/core/migrations/NNN_*.sql
   docker start odin
   docker logs --tail 30 odin    # verify boot succeeds
   ```
4. The `docker cp` is **ephemeral**. Next Watchtower reconcile (or manual `docker compose pull`) replaces the files with whatever the new image contains. So merge + push + tag the durable fix.

### 4.3 Specific error codes and first response

| `error.code` | Likely cause | First action |
|---|---|---|
| `itar_outbound_blocked` | A background task tried to call a public IP while `ODIN_ITAR_MODE=1` | Check `settings.spoolman_url`, webhooks, SMTP config; all must be private or ITAR mode disabled |
| `idempotency_conflict` | Two clients retrying with the same key on different payloads | Usually the client should mint a new key |
| `upstream_unavailable` | Printer unreachable from the container | Check printer power + LAN; may need to restart printer monitor |
| `quota_exceeded` | User hit their configured job quota | Bump `quota_jobs` in `users` table or change quota period |
| `dry_run_unsupported` | Agent sent `X-Dry-Run: true` to a route without opt-in | Retry without the header, OR upgrade backend to a version where the route opts in |

### 4.4 Rolling back

If a version is actively breaking prod, the rollback target is the
previous semver tag in GHCR (e.g., if `:latest` resolved to `:v1.9.4`
and that's broken, pin to `:v1.9.3`).

```bash
# 1. SSH to the Docker host
ssh root@<docker-host>
cd /opt/odin/runsodin   # path = directory containing your compose file;
                        # legacy installs use /opt/odin/runsodin/runsodin

# 2. Pin to the previous-known-good semver tag.
#    GHCR tags are :vX.Y.Z (no "-good" suffix). Pick the prior release
#    from `git tag -l 'v*' | sort -V | tail -5` or
#    https://github.com/HughKantsime/runsodin/releases.
GOOD_TAG=v1.9.3
sed -i "s|ghcr.io/hughkantsime/odin:.*|ghcr.io/hughkantsime/odin:${GOOD_TAG}|" \
    docker-compose.yml

# 3. Pull and recreate. Explicit `down && up -d` forces a clean restart
#    and exits any in-flight requests; safer under load than `up -d` alone.
docker compose pull
docker compose down
docker compose up -d

# 4. Verify. /health is always unauthenticated and reports the running
#    version. Do NOT use /api/v1/version here — it sits behind the
#    API_KEY perimeter and returns 401 to anonymous callers in prod.
curl -fsS http://localhost:8000/health | python3 -m json.tool
# expect: {"status":"ok","version":"1.9.3", ...}
```

**Watchtower interaction.** Once compose is pinned to `:vX.Y.Z`,
Watchtower checks for updates against that immutable tag and finds
none — it does not fight the pin. You do **not** need to stop
Watchtower to hold the rollback. To unpin later, revert the `sed` edit
(or restore `:latest`) once a fixed image has been published.

**Timing budget.** End-to-end ≤ 5 minutes from SSH connect to verified
`/health` on the rolled-back version. The dominant variable is
`compose pull` (GHCR network I/O); if that exceeds 60 s, log it.

**After a real rollback (not a drill).**
1. Comment on the deploy workflow run / release that prompted the
   rollback so the timeline is captured.
2. File a release-blocker issue against the version that was rolled
   back. Label `incident:rollback`.
3. Operator email + ntfy fire automatically via §3.1 / `deploy.yml`'s
   `notify` job, but a direct Hugh ping is still appropriate for any
   rollback that affected real users.
4. Schedule the post-mortem within 7 days.

The drill procedure that exercises this exact path lives in §4.6.

### 4.5 Restore from backup

```
# On the Proxmox host (or via web UI Datacenter → CT → Restore)
pct restore 999 /mnt/pve/<backup-target>/dump/vzdump-lxc-112-YYYY_MM_DD-*.tar.zst \
    --storage local-zfs

# Start as a scratch CT first — don't clobber prod blindly.
pct start 999
pct enter 999
# Verify the backup is sane (app starts, DB loads) before switching DNS.
```

### 4.6 Rollback drill — rehearse §4.4 before launch and quarterly

§4.4 is only worth what its last successful rehearsal proved. Drift
between this runbook and reality (paths, image tags, endpoint auth)
appears within weeks and gets discovered the first time it actually
matters — i.e., during a real outage. Drill it instead.

**Cadence.**
- **Pre-launch (one-time)**: Variant A (localhost) + Variant C (prod
  window). Tracked at ODIN-96.
- **Quarterly thereafter**: Variant A + tabletop diff against §4.4.
  Variant C only re-runs if deploy mechanism, image registry, or
  perimeter auth has changed since the last full prod rehearsal.

**What "drill succeeded" means.** All four:
1. A planted regression makes `/health` fail (any 5xx, timeout, or
   non-200) **or** `/health/ready` return 503.
2. Kuma fires the page within its threshold (2 consecutive failures
   on `odin.subsystem.app/health` → ~10 min worst case from regression
   to operator pager).
3. The §4.4 procedure restores `/health` 200 with the previous
   version within the 5-minute timing budget.
4. The drill log entry below is appended with measured times, who ran
   it, and any drift they had to work around.

**Variant A — localhost docker-compose drill (no prod risk).**

Run on any host with Docker + a GHCR PAT scoped to read packages.
Validates the §4.4 CLI mechanically against the real `:latest` image.

```bash
# In a scratch directory:
mkdir -p /tmp/odin-drill && cd /tmp/odin-drill
curl -O https://raw.githubusercontent.com/HughKantsime/runsodin/main/install/docker-compose.yml
echo "GHCR_PAT=$YOUR_PAT" | docker login ghcr.io -u <user> --password-stdin

# Boot at :latest, capture starting version.
docker compose up -d
sleep 30
curl -fsS http://localhost:8000/health | python3 -m json.tool   # version=N

# Plant the regression: pin to a tag known to break (or a deliberately
# stopped container — `docker compose stop odin` is the simplest fault
# injection because it produces 502/timeout, the same shape Kuma sees).
docker compose stop odin

# Time the rollback (start the clock here):
T0=$(date +%s)
GOOD_TAG=v1.9.3   # or the actual prior tag
sed -i.bak "s|ghcr.io/hughkantsime/odin:.*|ghcr.io/hughkantsime/odin:${GOOD_TAG}|" \
    docker-compose.yml
docker compose pull
docker compose down
docker compose up -d
until curl -fsS http://localhost:8000/health | grep -q '"status":"ok"'; do sleep 2; done
T1=$(date +%s)
echo "rollback elapsed: $((T1 - T0))s"
curl -fsS http://localhost:8000/health | python3 -m json.tool   # version=N-1

# Cleanup
docker compose down -v
```

**Variant B — demo stack drill (low-risk, ODIN Lead approval).**

The `demo.subsystem.app` stack is isolated from prod (separate compose
project, separate volume, separate posture per `ops/demo/README.md`).
Variant B exercises the same §4.4 commands against a real perimeter +
real DNS surface, without touching prod customers.

Constraints:
- Only run when App Review is **not** in an active reviewer window
  (per `ops/demo/README.md`, the probe is "only paged during App
  Review windows" — running a drill against demo during a review
  window risks confusing reviewers and burning their session).
- Need ODIN Lead acknowledgement before starting; not Hugh-only.

Procedure: same as Variant A, but the compose lives at
`/opt/odin-demo/docker-compose.demo.yml` on the M4 (or VPS, depending
on the variant from `ops/demo/README.md` §"Hosting variants"), and the
verify URL is `https://demo.subsystem.app/health`.

**Variant C — gated prod drill window (Hugh approval, planned outage).**

The only variant that proves the full chain — Watchtower + Kuma + ntfy
+ operator pager + the §4.4 path — works on the actual prod surface.

Hard preconditions (all required):
- Hugh has explicitly approved a planned ~30-minute window. This is
  the irreversible-blast-radius gate: a real customer hitting prod
  during the drill will see a real outage. Treat it like a maintenance
  window: announce, gate, then drill.
- The window is outside any peak operator hours (commonly Sunday
  morning local; confirm with Hugh).
- A child issue is open against ODIN-96 with the planned window, the
  go/no-go owner, and the rollback rollback (i.e., what to do if the
  drill itself can't restore from the planted bad version).

Procedure:
1. **Stage.** Build a deliberately-bad image — easiest is to push a
   one-line patch that makes `/health` return 500 — to a one-off tag
   like `:vX.Y.Z-drill-rc1` (do NOT push to `:latest`).
2. **Plant.** Manually `docker compose pull && up -d` against that
   `-drill-rc1` tag on prod. **Watchtower will not pull it because it
   wasn't pushed to `:latest`.**
3. **Watch.** Observe the time-to-page. Kuma should fire within ~10
   min (2 consecutive failures @ ≥ 60 s gap). Capture the page email +
   ntfy timestamps.
4. **Roll back.** Run §4.4 commands as written, pinning to the prior
   semver tag. Time the entire procedure.
5. **Confirm.** `/health` returns 200 with the prior version. Kuma
   monitor recovers. Send a "drill complete, prod restored" notice to
   the same channels.
6. **Log.** Append the drill log entry below. File any §4.4 drift as a
   follow-up issue and patch the runbook in the same PR.

**Drill log (append on each rehearsal):**

| Date | Variant | Operator | Time-to-page | Time-to-restore | Drift found | Notes |
|------|---------|----------|--------------|-----------------|-------------|-------|
| _yyyy-mm-dd_ | A / B / C | _name_ | _Mm Ss_ | _Mm Ss_ | _runbook line refs_ | _link to issue/PR_ |

---

## 5. Configuration knobs

Environment variables the backend reads at startup (set in
`docker-compose.yml` or the container env):

| Var | Default | Effect |
|---|---|---|
| `ENCRYPTION_KEY` | **required** | Fernet key for encrypted DB fields. Lost = lost access to SMTP/MQTT creds |
| `JWT_SECRET_KEY` | **required** | Signs JWTs from `/api/auth/login`. Rotation invalidates all sessions |
| `API_KEY` | "" | Optional global API key (perimeter auth). Blank = disabled |
| `DATABASE_URL` | `sqlite:////data/odin.db` | Only SQLite supported currently |
| `CORS_ORIGINS` | `localhost:8000,localhost:3000` | Add your real host |
| `ODIN_HOST_IP` | "" | Host LAN IP for WebRTC ICE — required for camera streaming |
| `TZ` | `America/New_York` | Log timestamps + scheduler |
| `ODIN_ITAR_MODE` | `0` | `1` enables fail-closed ITAR mode: boot audit + runtime DNS pin + no public egress |

**Secret storage**: `ENCRYPTION_KEY` and `JWT_SECRET_KEY` should ideally live in a secret manager (Vault, 1Password, etc.) and be injected at container start. Bare env values in `docker-compose.yml` on disk work but are less good.

**Rotating `JWT_SECRET_KEY`**: invalidates every active session and every JWT-derived auth. Users must log in again. Do this on a quiet window. Scoped tokens (`odin_xxx`) continue to work — they're hashed independently.

**Rotating `ENCRYPTION_KEY`**: breaks decryption of anything already encrypted. Requires running a re-encryption migration against the DB (not currently shipped — open a ticket before attempting).

---

## 6. Credentials and access

### 6.1 Admin account

The first user created via the setup wizard is the admin. Subsequent
admin accounts are created via the admin UI.

If you lose admin access: the WordPress-style first-user-wins
invariant means you can only add a new admin via an existing admin.
**Recovery path if all admins are locked out:** direct SQLite edit
against `/data/odin.db` to promote a user's `role` column. Document
this for yourself; do not rely on it.

### 6.2 Minting a scoped API token (for agents / service accounts)

```
# Get a JWT from your admin account
JWT=$(curl -sS -X POST https://<your-host>/api/auth/login \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d 'username=admin&password=YOUR_PASS' \
  | python3 -c 'import sys,json;print(json.loads(sys.stdin.read())["access_token"])')

# Mint a scoped token (v1.9.0+)
curl -sS -X POST https://<your-host>/api/v1/tokens \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{"name":"my-agent","scopes":["agent:write"],"expires_days":90}'
# → { "token": "odin_...", ... }
```

**Scope semantics:**
- `agent:read` — read-only surface (list/get). Viewer-role users can mint these.
- `agent:write` — the 11 advertised write tools. Only operator+ users can mint.
- `admin` — full surface. Only admins can mint.

Scoped tokens go in the `X-API-Key` header, not `Authorization: Bearer`.

### 6.3 Where credentials need to go

| What | Where | Why |
|---|---|---|
| Admin password | operator memory / password mgr | Login, token mint |
| SMTP password | admin UI → encrypted in DB | Alert emails |
| MQTT passwords | admin UI per-printer → encrypted in DB | Bambu printer connections |
| Camera URLs | admin UI per-printer → encrypted in DB | RTSP auth strings live here |
| GitHub PATs | CI secrets only, never the running container | Workflows only |
| Docker host SSH key | operator's own machine | Incident access |

---

## 7. Agent-surface operations (v1.9.0+)

### 7.1 Minimum viable agent smoke test

After any backend change, run this against prod (or a staging clone):

```
AGENT_TOKEN=odin_...   # minted per 6.2 with agent:write scope

# Reads — should all return 200
for ep in printers jobs alerts spools maintenance/tasks orders filament-library; do
  printf "GET /$ep "
  curl -sS -o /dev/null -w "%{http_code}\n" \
    -H "X-API-Key: $AGENT_TOKEN" \
    "https://<your-host>/api/v1/$ep"
done

# Writes — dry-run — should all return 2xx with dry_run:true body
curl -sS -X POST -H "X-API-Key: $AGENT_TOKEN" -H "X-Dry-Run: true" \
  -H "Content-Type: application/json" \
  -d '{"model_id":1,"item_name":"smoke","quantity":1,"priority":3}' \
  "https://<your-host>/api/v1/jobs"
# expect 201 with {"dry_run": true, "would_execute": {...}}
```

### 7.2 What X-Dry-Run does

`X-Dry-Run: true` on a mutating request returns what WOULD happen
without executing the side effect. Routes that haven't opted in
return **501 `dry_run_unsupported`** (the middleware refuses them)
rather than silently executing. Check the response envelope before
trusting that a write-previewed-as-OK will actually succeed — some
routes may have different validation at commit time.

### 7.3 Error envelope (agents branch on this)

Every 4xx / 5xx returns:
```
{
  "detail": "<legacy human message>",
  "error": {
    "code": "<stable snake_case id>",
    "detail": "<same message>",
    "retriable": true|false
  }
}
```

Retriable codes (client should back off + retry):
`upstream_unavailable`, `rate_limited`, `internal_error`.

Non-retriable (client must change input / auth):
`not_authenticated`, `scope_denied`, `printer_not_found`,
`invalid_state_transition`, `validation_failed`, `quota_exceeded`,
`idempotency_conflict`, `itar_outbound_blocked`,
`dry_run_unsupported`, plus the standard `*_not_found` family.

---

## 8. Appendix — command cookbook

### 8.1 Hot-patch a file in the running container (emergency)

```
scp local-fix.py root@<host>:/tmp/
ssh root@<host> 'docker cp /tmp/local-fix.py odin:/app/backend/<path>/<file>.py \
                && docker exec odin supervisorctl restart backend'
```

### 8.2 Inspect which migrations have applied

Migrations are re-run every boot (no tracking table). The container
logs each one at startup:
```
docker logs odin 2>&1 | grep "Applied core migration"
```

### 8.3 Dump the DB

```
docker exec odin sqlite3 /data/odin.db '.backup /data/odin.db.backup'
docker cp odin:/data/odin.db.backup ./odin-$(date +%Y%m%d).db
```

### 8.4 Rotate an admin password

```
# Preferred: via the admin UI → Users → Edit → Set Password.
# Emergency fallback (lost admin creds):
ssh root@<host> 'docker exec -it odin sqlite3 /data/odin.db'
# Inside sqlite3:
UPDATE users SET password_hash = '<bcrypt-hashed-new-password>'
  WHERE username = 'admin';
.quit
```
Generate bcrypt hash: `python3 -c "import bcrypt; print(bcrypt.hashpw(b'NEW_PASSWORD', bcrypt.gensalt(rounds=12)).decode())"`

### 8.5 Check current backup freshness

```
ssh root@<pve-host> 'pvesm list proxmox_nas_backup --content backup --vmid 112'
# Most recent row should be from within the last 24 hours.
```

### 8.6 Force Watchtower to reconcile right now

```
# Watchtower watches labeled containers. Trigger an immediate check:
docker exec watchtower /watchtower --run-once odin
# Or restart watchtower, which triggers its poll loop:
docker restart watchtower
```

### 8.7 Full sweep against the agent surface (expect all green)

See section 7.1. Run on every release against the production URL with
`X-Dry-Run: true` on writes. Takes ~30 seconds. If any route returns
non-2xx, capture the traceback from `/data/backend.log` and file it.

---

## 9. What this runbook does NOT cover

- **Customer data export** — GDPR export lives under admin UI → Users → Export. Document the specific compliance regime (GDPR / CCPA / etc.) for your deployment separately.
- **HA / multi-instance** — ODIN is currently single-container SQLite. There is no horizontal scaling path in this release. If you need it, plan a Postgres migration + the associated ops burden.
- **Network topology** — assumes your reverse proxy, firewall, and DNS are already set up. This doc only covers what lives between the proxy and the database.
- **Customer onboarding / setup-wizard flow** — covered in the user-facing docs at `docs.<your-domain>/setup`.

---

## 10. When this runbook lies

Anything in this document is true at the time it was last edited. If
you find a command that no longer works, a path that's been moved, or
a behavior that doesn't match reality — **fix the runbook in the same
change that fixes the drift**. A stale runbook is worse than no
runbook, because it masquerades as authority.

Last rev: 2026-04-16. Maintained alongside the ODIN release cycle.
