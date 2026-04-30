# `demo.subsystem.app` — App Review reviewer environment

Per ODIN-128 / ODIN-125 plan v2 (ratified by Hugh 2026-04-30, Option 1 +
reuse-existing-replayer amendment).

This stack is **isolated** from prod ODIN. It exists to give App Review
reviewers a working, "live" printer to inspect from the iOS / macOS app,
without exposing any real customer data, real homelab IPs, or real
printer credentials.

## Architecture (no greenfield — wiring only)

```
                    ┌────────────────────────────┐
                    │  demo.subsystem.app (TLS)  │  ← Caddy / Cloudflare
                    └─────────────┬──────────────┘
                                  │  127.0.0.1:8000
                  ┌───────────────┴───────────────┐
                  │  odin (compose service `odin`)│  ← reuses prod image
                  │   ENV: ODIN_DEMO_MODE=1       │
                  │        ODIN_BAMBU_BROKER_URL=mqtt://mosquitto:1883
                  └───────────────┬───────────────┘
                                  │ MQTT subscribe
                                  │ device/00M09D4B1600284/report
                  ┌───────────────┴───────────────┐
                  │  mosquitto (compose service)  │  ← eclipse-mosquitto:2.0
                  └───────────────┬───────────────┘
                                  │ MQTT publish
                  ┌───────────────┴───────────────┐
                  │  publisher (compose service)  │  ← ops/demo/demo_publisher.py
                  │  loops bambu-x1c-ams-swap.demo.jsonl  │
                  └───────────────────────────────┘
```

The publisher reuses `publish_fixture` from
`backend/modules/printers/telemetry/live_replay.py` — the same code path
exercised by integration tests. **No new state-machine code; no new
telemetry-handling code.**

## Deliverables (ODIN-128 status)

| # | Deliverable                                          | Status                                                                   |
|---|------------------------------------------------------|--------------------------------------------------------------------------|
| 1 | `demo.subsystem.app` DNS / TLS / perimeter routing   | **Hugh** — Cloudflare DNS + cert + Caddy/Traefik proxy on chosen host    |
| 2 | Isolated FastAPI / Postgres / mosquitto stack        | `ops/demo/docker-compose.demo.yml`                                       |
| 3 | `ams-swap-loop` scenario shim                        | `demo_scenarios/ams-swap-loop/scenario.yaml` + `loop:true` in `demo.py`  |
| 4 | Identity-scrub audit on chosen fixture               | `ops/demo/scrub_fixture.py`; scrubbed → `bambu-x1c-ams-swap.demo.jsonl`  |
| 5 | Reviewer account `appreview@demo.subsystem.app`      | `make demo-rotate-password` + `make demo-write-credentials`              |
| 6 | Nightly reset cron                                   | `make demo-reset` target (compose-down → wipe → rotate → up)             |
| 7 | Health probe + ntfy alerting                         | `ops/demo/health_probe.py` (paging filter is caller-side)                |
| 8 | Hand verified URL + creds back to ODIN Lead          | Pending DNS / first deploy                                               |

## Identity-scrub audit (ODIN-128 deliverable 4)

The committed source fixture
`tests/fixtures/telemetry/bambu-x1c-ams-swap.jsonl` has its identity
synthesized for the printer serial (`00M09D4B1600284` is not a real
Bambu SN) but **does** contain a real homelab LAN IP and one real
customer print-job filename. `ops/demo/scrub_fixture.py` rewrites those
into `bambu-x1c-ams-swap.demo.jsonl` (committed, used by this demo).

Audit run on the source fixture (54 events):

| Field / pattern                   | Source value                                          | Replaced with                  |
|-----------------------------------|--------------------------------------------------------|--------------------------------|
| `print.ipcam.rtsp_url`            | `rtsps://192.168.72.210:322/streaming/live/1`         | `rtsps://192.168.1.42:322/...` |
| `print.ip` (Bambu LE uint32)      | `3527977152` (= `192.168.72.210`)                     | `704751808` (= `192.168.1.42`) |
| `print.subtask_name`              | `cabinet_lock_v3.1`                                    | `demo_widget_v1`               |
| `print.upgrade_state.sn` / topic  | `00M09D4B1600284`                                      | unchanged (already synthetic)  |

Negative checks (no matches in source, no scrub needed):

- emails (`/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/`) — none
- JWTs (`/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/`) — none
- MAC addresses — none
- payload keys checked: `wifi_signal, ssid, lan_ip, ip_address, user, username, owner, operator, nickname, token, access_token, auth_token, bearer, session_id, email, phone, mac_address, cloud_token, secret, password, lat, latitude, lon, longitude, host, hostname, fqdn` — only `wifi_signal: -66dBm` matched, which is signal strength (non-identifying).

Re-run audit any time:

```sh
python ops/demo/scrub_fixture.py --audit-only           # source
python ops/demo/scrub_fixture.py --write                # rewrite scrub
```

## Operating

```sh
cd ops/demo
cp env.example .env             # then edit secrets — never share with prod
make -f Makefile.demo demo-build
make -f Makefile.demo demo-up
make -f Makefile.demo demo-probe
```

### Nightly reset (cron 03:00 UTC)

```cron
0 3 * * * cd /opt/odin-demo && /usr/bin/make -f ops/demo/Makefile.demo demo-reset >> /var/log/odin-demo-reset.log 2>&1
```

`demo-reset`:

1. `docker compose down` — stops all three services.
2. Wipes `./odin-demo-data/` and `./heartbeat/` (no shared data with prod).
3. Generates a fresh 12-char alphanumeric reviewer password and writes
   `ops/demo/.reviewer-credentials.txt` (gitignored).
4. `docker compose up -d` — restarts mosquitto + odin + publisher.

Target wall-clock: ≤60s. Hand the new password back to the ASC paste-block
(see ODIN-125).

### Health probe

`ops/demo/health_probe.py` checks:

1. `GET <base-url>/login` returns HTTP 200.
2. mosquitto port reachable.
3. Publisher heartbeat younger than `--max-stale-sec` (default 30s).

Wire into ntfy by piping JSON output through `ops/demo/probe_ntfy.sh`
(operator-supplied; not committed). Probe is **only paged during App
Review windows** — the pager filter lives outside this script.

## Hard requirements (ITAR/CMMC, non-negotiable)

- Zero real customer data — see scrub above.
- Zero real homelab IPs — scrubbed.
- Zero real printer credentials — anonymous mosquitto, no Bambu cloud token in env.
- No shared persistence with prod ODIN — separate compose project, separate volume.
- No GHCR / ASC / prod-X-API-Key — `ops/demo/.env` must be a fresh keypair.
- No inbound MQTT from real printers — mosquitto is on the `demo` bridge network only.
- `noindex` — `robots.txt` in this directory must be served at the demo origin root.

## Hugh-handoff checklist (deliverable 1)

Cannot be done from this seat without account access:

- [ ] Cloudflare DNS: `demo.subsystem.app` → host IP (and AAAA if v6).
- [ ] TLS cert (Cloudflare Universal SSL or `caddy` automatic-HTTPS).
- [ ] Perimeter proxy (Caddy / Traefik) terminating TLS, proxying to
      `127.0.0.1:8000` on the chosen host (M4 or VPS).
- [ ] Hosting decision: M4 separate compose project (preferred per
      issue) or ≤US$10/mo VPS.
- [ ] First deploy: `cp env.example .env; edit; make demo-up`.

When DNS resolves and `make demo-probe-public` is green, comment back on
ODIN-128 and ODIN-125 with the verified URL + current contents of
`ops/demo/.reviewer-credentials.txt`.
