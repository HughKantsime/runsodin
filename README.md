<p align="center">
  <img src="docs/images/odin-banner.png" alt="O.D.I.N." width="600" />
</p>

<h1 align="center">O.D.I.N.</h1>
<p align="center"><strong>Orchestrated Dispatch & Inventory Network</strong></p>
<p align="center">Self-hosted 3D print farm management for people who own their data.</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> •
  <a href="#features">Features</a> •
  <a href="#supported-printers">Printers</a> •
  <a href="#screenshots">Screenshots</a> •
  <a href="#license">License</a> •
  <a href="https://discord.gg/kZna6rex">Discord</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-0.19.0-blue" alt="Version" />
  <img src="https://img.shields.io/badge/license-BSL%201.1-green" alt="License" />
  <img src="https://img.shields.io/badge/python-3.11+-yellow" alt="Python" />
  <img src="https://img.shields.io/badge/RAM-~400MB-orange" alt="RAM" />
</p>

---

## What is O.D.I.N.?

O.D.I.N. is a self-hosted MES (Manufacturing Execution System) for 3D print farms. It monitors your printers in real time, manages job queues, tracks filament inventory, and gives you per-order profitability — all without sending a single byte to the cloud.

Built for hobbyists running Etsy shops, schools with printer labs, and defense contractors who need ITAR-compliant manufacturing software. Runs on a Raspberry Pi, a mini PC, or any machine with Docker.

**O.D.I.N. is not a slicer.** You slice in Bambu Studio or OrcaSlicer, upload the `.3mf` to O.D.I.N., and it handles everything from there.

---

## Quick Start

### Docker (recommended)

```bash
git clone https://github.com/HughKantsime/printfarm-scheduler.git odin
cd odin
cp .env.example .env
docker-compose up -d
```

Open `http://localhost:8000` — the setup wizard walks you through creating an admin account and connecting your first printer. Takes about 2 minutes.

### Manual Install

See [docs/manual-install.md](docs/manual-install.md) for bare-metal installation on Ubuntu/Debian.

---

## Features

### Dashboard & Monitoring
- **Live printer status** — bed/nozzle temps, print progress, time remaining, all updated via MQTT
- **Progress bars with countdown** — see exactly how long each print has left
- **Low spool warnings** — amber indicators when filament drops below 100g
- **Camera grid** — live feeds from all printers via WebRTC (go2rtc)
- **Control Room mode** — full-screen camera wall with clock overlay (press F)
- **Fleet status** — sidebar widget shows online printer count at a glance
- **Emergency stop** — floating button to stop/pause/resume any active print

### Job Management
- **Smart scheduler** — color-match scoring to minimize filament swaps
- **Upload → Schedule workflow** — drop a `.3mf`, metadata auto-extracts, schedule in one click
- **Print Again** — one-click clone of completed jobs
- **Order tracking** — link jobs to customer orders for fulfillment visibility
- **Job tabs** — filter by All / Order Jobs / Ad-hoc
- **Timeline view** — Gantt-style visualization of your print queue

### Filament & Inventory
- **AMS RFID auto-tracking** — Bambu AMS spools detected and tracked automatically
- **QR code scanner** — assign spools to non-RFID printer slots via camera or manual entry
- **Auto-deduct on complete** — filament weight updates automatically when jobs finish
- **Spool library** — full CRUD with brand, material, color, weight, cost tracking

### Products & Orders
- **Product catalog with BOM** — define what you sell and what prints make it up
- **Order management** — track orders from Etsy, Amazon, wholesale, or direct
- **Per-order P&L** — revenue, platform fees, payment fees, shipping, filament cost, labor → profit and margin
- **Fulfillment tracking** — auto-progress orders as linked jobs complete

### Cost & Analytics
- **Pricing calculator** — filament, electricity, depreciation, labor, markup
- **Per-material cost rates** — different $/gram for PLA, PETG, ASA, etc.
- **Model cards show cost** — estimated cost and suggested price on every model
- **Revenue dashboard** — margins, costs, and profitability from real job data
- **CSV export** — jobs, models, spools, filament usage

### Multi-User & Security
- **JWT authentication** with role-based access (admin / operator / viewer)
- **RBAC permissions** — visual role matrix with per-action toggles
- **SSO/OIDC** — Microsoft Entra ID, with auto-user provisioning
- **White-label branding** — custom colors, fonts, logos, app name
- **Encrypted credentials** — printer API keys stored with Fernet encryption

### Notifications
- **Browser push** — VAPID-based notifications via service worker
- **Webhooks** — Discord and Slack integration with alert type filtering
- **Email** — SMTP-based alerts for print complete, failures, maintenance due
- **In-app alerts** — bell icon with unread count, filterable alerts page
- **ntfy + Telegram** — lightweight push via ntfy.sh or Telegram Bot API
- **Quiet hours** — suppress notifications overnight, get a daily digest instead

### Integrations & Monitoring
- **MQTT republish** — forward printer events to external broker for Home Assistant, Node-RED, Ignition
- **Prometheus /metrics** — expose telemetry for Grafana dashboards
- **Smart plug control** — Tasmota, Home Assistant, or MQTT-based power management with auto on/off
- **Energy tracking** — per-job electricity cost via smart plug kWh monitoring
- **AMS environment** — humidity and temperature monitoring with 7-day history
- **WebSocket** — real-time push updates (no more polling)

### 3D & UX
- **3D model viewer** — interactive Three.js preview of .3mf files with orbit controls
- **Drag-and-drop queue** — reorder print jobs by dragging
- **Keyboard shortcuts** — global hotkeys with ? help modal
- **PWA support** — install as a native app on mobile and desktop
- **Multi-language** — English, Deutsch, 日本語, Español (community contributions welcome)

### Maintenance
- **Care counters** — total print hours, print count, hours/prints since last maintenance
- **Task templates** — define recurring maintenance tasks
- **Maintenance history** — log when work was performed

---

## Supported Printers

| Printer | Protocol | Status |
|---------|----------|--------|
| Bambu Lab X1C | MQTT | ✅ Full support (AMS, cameras, lights, HMS alerts) |
| Bambu Lab P1S | MQTT | ✅ Full support |
| Bambu Lab A1 | MQTT | ✅ Full support |
| Bambu Lab A1 Mini | MQTT | ✅ Full support |
| Bambu Lab H2D | MQTT | ✅ Full support |
| Klipper/Moonraker | REST | ✅ Supported (Anycubic Kobra S1 w/ Rinkhals tested) |
| PrusaLink | REST | 🔜 Planned |
| Elegoo | — | 🔜 Planned |

O.D.I.N. is brand-agnostic by design. If your printer speaks MQTT or has a REST API, it can be integrated.

---

## Screenshots

> Screenshots coming soon. In the meantime, check the [demo video](https://youtube.com/YOUR_VIDEO).

<!-- 
<details>
<summary>Dashboard</summary>
<img src="docs/images/dashboard.png" alt="Dashboard" />
</details>

<details>
<summary>Camera Grid</summary>
<img src="docs/images/cameras.png" alt="Cameras" />
</details>

<details>
<summary>Orders & P&L</summary>
<img src="docs/images/orders.png" alt="Orders" />
</details>
-->

---

## Architecture

```
React 18 + Vite + TailwindCSS (frontend)
       ↕ /api proxy
FastAPI + SQLite WAL (backend)
       ↕
MQTT (Bambu) + Moonraker (Klipper) + go2rtc (cameras)
```

Single container. ~400MB RAM. SQLite database — no Postgres, no Redis, no message queue. The entire system fits on a Raspberry Pi 5 or an $80 Intel N100 mini PC.

---

## Configuration

All configuration is via environment variables (`.env` file). On first run with Docker, secrets are auto-generated and persisted to `odin-data/`.

| Variable | Required | Description |
|----------|----------|-------------|
| `ENCRYPTION_KEY` | Auto | Fernet key for encrypting printer credentials |
| `JWT_SECRET_KEY` | Auto | Secret for signing JWT tokens |
| `API_KEY` | No | API key for frontend auth (blank = disabled) |
| `TZ` | No | Timezone (default: `America/New_York`) |
| `CORS_ORIGINS` | No | Allowed origins for CORS |

---

## Licensing

O.D.I.N. is source-available under the [Business Source License 1.1](LICENSE).

- **Free for personal and non-commercial use** (Community Edition — up to 5 printers, single user)
- **Commercial use requires a paid license** (Pro, Education, or Enterprise)
- **Each version converts to Apache 2.0 after 3 years**

### Tiers

| | Community | Pro | Education | Enterprise |
|---|---|---|---|---|
| **Price** | Free | $20/mo | $499 appliance + $300/yr | Custom |
| **Printers** | 5 | Unlimited | Unlimited | Unlimited |
| **Users** | 1 | Unlimited | Unlimited | Unlimited |
| **SSO/OIDC** | — | ✅ | ✅ | ✅ |
| **Orders & BOM** | — | ✅ | ✅ | ✅ |
| **Webhooks & Email** | — | ✅ | ✅ | ✅ |
| **White-label** | — | ✅ | ✅ | ✅ |
| **Job Approval** | — | — | ✅ | ✅ |
| **MQTT Republish** | — | ✅ | ✅ | ✅ |
| **OPC-UA** | — | — | — | ✅ |
| **Prometheus Metrics** | — | ✅ | ✅ | ✅ |
| **Smart Plug Control** | — | ✅ | ✅ | ✅ |
| **Audit Export** | — | — | — | ✅ |
| **Support** | Community | Email | Email + Onboarding | SLA |

License keys are air-gap friendly — a signed file dropped into your install. No phone home, no cloud validation.

→ [runsodin.com](https://runsodin.com) for pricing and purchase.

---

## What O.D.I.N. Is Not

- **Not a slicer** — use Bambu Studio, OrcaSlicer, or PrusaSlicer
- **Not a cloud service** — your data stays on your machine, always
- **Not an ERP** — export to QuickBooks/Xero for accounting
- **Not a file sender** — O.D.I.N. observes and manages, it doesn't push files to printers

---

## Community

- 💬 [Discord](https://discord.gg/kZna6rex) — help, feature requests, show your setup
- 🐛 [GitHub Issues](https://github.com/HughKantsime/printfarm-scheduler/issues) — bug reports
- 🌐 [runsodin.com](https://runsodin.com) — docs, pricing, updates

---

## Contributing

O.D.I.N. is source-available, not open source (yet). Each version converts to Apache 2.0 after 3 years.

Bug reports and feature requests are welcome via GitHub Issues. If you'd like to contribute code, please open an issue first to discuss.

---

## Acknowledgments

Built by [Sublab 3DP](https://sublab3dp.com) in Knoxville, TN.

Named for the All-Father — because your print farm deserves someone watching over it.

---

<p align="center">
  <sub>O.D.I.N. — Orchestrated Dispatch & Inventory Network</sub><br/>
  <sub>© 2026 Sublab 3DP. All rights reserved.</sub>
</p>
