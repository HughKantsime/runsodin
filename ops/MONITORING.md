# O.D.I.N. Post-Launch Monitoring

Canonical list of URLs to watch, what watches them, where alerts go, and the thresholds at which a human gets paged. Pair with `ops/RUNBOOK.md` for incident response.

If you change a monitor or threshold, update this file in the same change. A stale monitoring doc is the failure mode that masquerades as coverage.

Last rev: 2026-04-30.

---

## 1. URL list (what to watch)

| Surface | URL | Auth | Expected response | Owner |
|---|---|---|---|---|
| Backend liveness | `https://odin.subsystem.app/health` | none | `200` + `{"status":"ok","version":"…"}` | core platform |
| Backend readiness | `https://odin.subsystem.app/api/v1/health/ready` | API key | `200` + `{"ready":true,"version":"…"}` | core platform |
| Backend version | `https://odin.subsystem.app/api/v1/version` | API key | `200` + `{"version":"…"}` matches `:latest` | core platform |
| Marketing site root | `https://runsodin.com/` | none | `200`, HTML, includes `<title>O.D.I.N.</title>` | core platform / marketing |
| Marketing site liveness | `https://runsodin.com/api/health` | none | **NOT YET DEPLOYED** — see ODIN-95 child issue | core platform |
| Vercel KV cron heartbeat | KV key `cron:close-tickets:last-run` | KV admin | timestamp ≤ 2× cron interval | core platform |
| Kuma admin | `https://kuma.subsystem.app/` | login | dashboard reachable | infra |
| Public status page | `https://status.runsodin.com/` (or `kuma.subsystem.app/status/<slug>`) | none | **NOT YET CREATED** — see child issue | infra / marketing |
| TLS cert (backend) | `odin.subsystem.app:443` | none | cert valid > 14 days | infra |
| TLS cert (site) | `runsodin.com:443` | none | cert valid > 14 days | infra |

---

## 2. Alert thresholds

These are the numbers at which a human is woken up. Anything noisier belongs in a daily digest, not a page.

### 2.1 Uptime

| Surface | Page when | Notify | Channel |
|---|---|---|---|
| `odin.subsystem.app/health` | 2 consecutive failures (≥ 60 s gap) | operator email + ntfy | Kuma → SMTP + ntfy webhook |
| `runsodin.com/` | 3 consecutive failures (≥ 60 s gap) | operator email | Kuma → SMTP |
| Vercel cron `close-tickets` | KV heartbeat older than 2× expected interval | operator email | Kuma push monitor → SMTP |
| TLS cert (any surface) | < 14 days to expiry | operator email | Kuma cert monitor (built-in) |

**Why 2 vs 3 failures:** the backend is the operational surface — false-positive cost is low because the operator already pays attention to it. The marketing site is read-mostly and CDN-cached; one transient failure mid-deploy is normal noise.

### 2.2 Error rate

| Surface | Page when | Source | Channel |
|---|---|---|---|
| Backend 5xx rate | > 1% of requests over a 10-min window | **NOT YET INSTRUMENTED** — needs log shipping or APM (see child issue) | TBD |
| Backend `error.code` spike | any new code appearing > 10×/hr that wasn't seen the prior hour | same as above | TBD |
| Vercel function errors | > 5 errors in any 5-min window per function | Vercel dashboard alerting (built-in) — needs configuration | Vercel email + Slack-equivalent |
| Cert-manager renewal failure | any failure | k3s cluster events | k8s alert → ntfy (TBD) |

**Why no Sentry-class APM yet:** ODIN backend logs to `/data/backend.log` only. Adding Sentry/GlitchTip is a discrete decision (see ODIN-95 child issue: "Pick error tracker for backend + Vercel functions"). Until then, error rate is observed by tailing logs during incidents — that is **not coverage**, it is just availability of evidence after the fact.

### 2.3 Install success rate

| Surface | Page when | Source | Channel |
|---|---|---|---|
| `install-smoke` CI workflow | failure on `main` or any PR touching `install/**` or `backend/**` | GitHub Actions | GitHub email + ntfy on release pipeline |
| Real-world install completion | < 80% over rolling 7-day window | **NOT YET INSTRUMENTED** — installer is local-only with no phone-home | TBD (see child issue: "Decide install completion telemetry") |
| GHCR pull count drop | > 50% week-over-week | GHCR metrics | none yet (manual dashboard) |
| Support ticket spike with `install` keyword | > 3 in 24h | runsodin.com chatbot → GitHub Issues | GitHub email + ntfy |

**Why no real-world install rate:** `install/install.sh` does not phone home. Designing a privacy-respecting opt-in install ping is a real product decision (ITAR/CMMC posture matters). Until then, **install success rate is the CI smoke pass rate plus the support-ticket signal** — which understates real friction. Document this gap honestly; do not claim coverage we do not have.

---

## 3. Alert wiring (where pages land)

```
                    ┌──────────────────────────────────┐
                    │  Kuma (kuma.subsystem.app)       │
                    │  ─ HTTP monitors                 │
                    │  ─ Cert expiry monitors          │
                    │  ─ Push (heartbeat) monitors     │
                    └──────────┬───────────────────────┘
                               │ on failure ≥ threshold
                               ▼
                    ┌──────────────────────────────────┐
                    │  Notification channels:          │
                    │   1. SMTP → operator email       │
                    │   2. ntfy webhook → mobile push  │
                    └──────────────────────────────────┘

  GitHub Actions ──► ntfy push (deploy.yml line 329 + verify-prod result)

  Vercel functions ─► Vercel dashboard alerts (TBD wiring)
                   └► /api/cron/close-tickets writes KV cron:close-tickets:last-run
                      → Kuma push monitor reads or expects heartbeat

  Backend logs ──► /data/backend.log (host-local; no shipper yet)
```

**Silent-monitor trap (load-bearing):** a Kuma monitor can exist and record heartbeats *without* being attached to a notification channel. Prod went 19h unnoticed once because the monitor-to-channel link was missing. Whenever you add a monitor, also click into it and confirm the notification list is non-empty.

**Notification channel test cadence:** send a deliberate failing test once per quarter to confirm both SMTP and ntfy actually deliver. Calendar this; do not trust "it worked last time."

---

## 4. What is verified by CI vs. what runs in prod

| Check | Where it runs | What it catches |
|---|---|---|
| `install-smoke` workflow | GitHub Actions on PR | Installer breakage on clean Ubuntu — does **not** catch real-world install friction |
| `verify-prod` workflow | GitHub Actions post-deploy | New `:latest` actually serves the expected version within 10 min |
| `prod_verify_public.sh` | Same as above (no secrets) | `/health` + `/api/v1/health/ready` for the deployed version |
| Kuma HTTP monitor on `/health` | Kuma instance | Continuous availability between deploys |
| Kuma cert monitor | Kuma instance | TLS expiry warning ≥ 14d out |
| Backend supervisord | Inside container | Worker process restarts (silent unless tailing logs) |
| Watchtower | Docker host | Auto-pulls new `:latest`; does **not** verify the new version is healthy — that's what `verify-prod` is for |

CI catches regressions on the change-set. Kuma catches drift between deploys. Neither catches user-facing error rate today — that is the open gap.

---

## 5. Adding a monitor — checklist

Whenever you wire a new monitor:

1. **Define the threshold first.** What error rate / latency / window pages a human? Numbers, not adjectives.
2. **Pick a notification channel** before you save the monitor. Re-confirm it is attached after save (silent-monitor trap).
3. **Send a deliberate failing test** within 5 minutes of activation to confirm the page actually arrives.
4. **Update this file** with the new entry under §1, the threshold under §2, and the channel under §3.
5. **Annotate the runbook** if the monitor implies a new incident-response branch.

---

## 6. Open gaps (tracked as ODIN-95 child issues)

The four items below are deliberately *not* fixed in this doc revision because each is its own bounded change with a specific decision attached. Linked in the ODIN-95 thread.

1. **Add `/api/health` to `runsodin.com`** (Vercel function returning 200 + last cron heartbeat) — code-only, low blast radius.
2. **Wire Kuma monitors** for `runsodin.com/`, `runsodin.com/api/health`, and a public status page slug — needs Kuma admin login.
3. **Pick a backend + Vercel error tracker** (Sentry / GlitchTip / log shipper) — needs Hugh approval (paid SaaS or self-hosted ops cost).
4. **Decide install completion telemetry** (opt-in ping vs. proxy via GHCR pulls + support tickets) — product/privacy decision; ITAR/CMMC posture matters.

---

## 7. When this doc lies

If a monitor listed here is silent, missing, or pointing at a defunct channel, **fix it in the same change that fixes the drift**. The runbook is true at the time it was last edited; this doc inherits the same contract.
