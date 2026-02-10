<div align="center">

# O.D.I.N.

**Orchestrated Dispatch & Inventory Network**

Self-hosted 3D print farm management for production environments.

[![Version](https://img.shields.io/badge/version-1.0.8-blue)](https://github.com/HughKantsime/runsodin/releases)
[![Tests](https://img.shields.io/badge/tests-1031%20passed-brightgreen)](#)
[![License](https://img.shields.io/badge/license-BSL%201.1-orange)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11-blue)](https://python.org)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED)](https://github.com/HughKantsime/runsodin/pkgs/container/odin)
[![RAM](https://img.shields.io/badge/RAM-256MB-green)](#)

[Website](https://runsodin.com) · [Discord](https://discord.gg/odin-community) · [Documentation](#documentation) · [Quick Start](#quick-start)

</div>

---

## What is O.D.I.N.?

O.D.I.N. is a self-hosted print farm management system built for people who run 3D printers as a business — not a hobby. It connects to your printers over your local network, tracks jobs and inventory, manages orders, and gives you a real-time dashboard of your entire fleet.

**No cloud. No subscriptions required. No data leaves your network.**

### Who it's for

- **Commercial print farms** running mixed fleets of Bambu, Prusa, Klipper, and Elegoo printers
- **Defense contractors & manufacturers** needing ITAR/CMMC-compliant air-gapped operations
- **Schools & makerspaces** managing student access to shared printer fleets
- **Serious hobbyists** who outgrew manufacturer apps

### What it's not

O.D.I.N. is an observer and scheduler — it monitors your printers and manages the business around them. It does not slice files or send prints to printers. You still use Bambu Studio, OrcaSlicer, or PrusaSlicer for that.

---

## Features

### Fleet Management
- Real-time dashboard with live printer status, temperatures, and progress
- Multi-vendor support: Bambu Lab, Klipper/Moonraker, PrusaLink, Elegoo SDCP
- Live camera streaming via go2rtc (RTSP, WebRTC)
- Control room mode (full-screen camera grid)
- Emergency stop button accessible from any screen
- AMS filament slot tracking with humidity/temperature monitoring
- HMS error code translation (42 Bambu error codes decoded)

### Business Operations
- Orders, products, and bill of materials (BOM) management
- Cost calculator with configurable pricing (material, time, markup)
- Spool inventory tracking with usage history
- Print job scheduling with drag-and-drop queue
- Printer utilization and cost/revenue reports with CSV export
- Per-job energy consumption tracking (kWh + cost)

### Security & Access Control
- Role-based access control (Viewer / Operator / Admin) on all 164 API endpoints
- OIDC/SSO integration (Entra ID, Google, Okta, any OIDC provider)
- Encrypted printer credentials (Fernet)
- Audit logging with searchable history
- Rate limiting and account lockout
- Air-gap friendly — no internet required after installation

### Notifications
- Browser push notifications
- Email (SMTP)
- Discord and Slack webhooks
- ntfy and Telegram
- Custom webhooks for any integration
- Quiet hours with daily digest

### Integrations
- Prometheus `/metrics` endpoint (Grafana-ready)
- MQTT republish to external brokers (Home Assistant, Node-RED, Ignition)
- REST API with Swagger documentation at `/api/docs`
- WebSocket real-time updates
- Smart plug control (Tasmota, Home Assistant, MQTT)

### Quality of Life
- First-run setup wizard (admin account → printer → network → done)
- PWA support (add to homescreen on mobile)
- Keyboard shortcuts (press `?` to see them all)
- Multi-language UI (English, German, Japanese, Spanish)
- White-label branding (custom name, logo, colors)
- Dark theme by default

---

## Supported Printers

| Protocol | Printers | Status |
|----------|----------|--------|
| **Bambu MQTT** | X1 Carbon, A1, P1S, H2D | ✅ Full support |
| **Moonraker** | Any Klipper printer — Voron, Creality K1, QIDI, Sovol, Anycubic | ✅ Full support |
| **PrusaLink** | MK4/S, MK3.9, MK3.5, MINI+, XL, CORE One | ✅ Supported |
| **Elegoo SDCP** | Centauri Carbon, Neptune 4, Saturn series | ✅ Supported |
| **OctoPrint** | Legacy printers | Planned |

---

## Quick Start

### Docker (Recommended)

```bash
mkdir odin && cd odin
curl -O https://raw.githubusercontent.com/HughKantsime/runsodin/master/install/docker-compose.yml
docker compose up -d
```

Open `http://your-server-ip:8000` and follow the setup wizard.

That's it. One container, ~256MB RAM, all services included.

### What's in the box

The single Docker container runs:
- **Backend** — FastAPI on port 8000 (serves API + built frontend)
- **MQTT Monitor** — Bambu printer telemetry
- **Moonraker Monitor** — Klipper printer telemetry
- **PrusaLink Monitor** — Prusa printer telemetry
- **Elegoo Monitor** — Elegoo printer telemetry
- **go2rtc** — Camera streaming relay (ports 1984, 8555)

### Updating

```bash
docker compose pull
docker compose up -d
```

Your data lives in the `./odin-data` volume and survives updates.

### Development Setup

```bash
git clone https://github.com/HughKantsime/runsodin.git
cd runsodin
docker compose up -d --build
```

Builds from source. Changes to `backend/` or `frontend/` require a rebuild.

---

## Architecture

```
┌─────────────────────────────────────────────┐
│              Docker Container               │
│                                             │
│  ┌─────────┐  ┌──────────┐  ┌───────────┐  │
│  │ FastAPI  │  │   MQTT   │  │ Moonraker │  │
│  │ :8000   │  │ Monitor  │  │  Monitor  │  │
│  └────┬────┘  └────┬─────┘  └─────┬─────┘  │
│       │            │               │        │
│  ┌────┴────────────┴───────────────┴────┐   │
│  │          SQLite (WAL mode)           │   │
│  └──────────────────────────────────────┘   │
│                                             │
│  ┌──────────┐  ┌───────────┐  ┌──────────┐ │
│  │ PrusaLink│  │  Elegoo   │  │  go2rtc  │ │
│  │ Monitor  │  │  Monitor  │  │  :1984   │ │
│  └──────────┘  └───────────┘  └──────────┘ │
└─────────────────────────────────────────────┘
         │              │              │
    ┌────┴────┐   ┌─────┴─────┐  ┌────┴────┐
    │  Bambu  │   │  Klipper  │  │  Prusa  │
    │ Printers│   │  Printers │  │ Printers│
    └─────────┘   └───────────┘  └─────────┘
```

- **Backend**: Python 3.11 / FastAPI / SQLAlchemy
- **Frontend**: React 18 / Vite / TailwindCSS
- **Database**: SQLite with WAL mode (handles concurrent reads/writes)
- **Camera streaming**: go2rtc with WebRTC and RTSP support

---

## Pricing

| Tier | Price | Printers | Users |
|------|-------|----------|-------|
| **Community** | Free | 5 | 1 |
| **Pro** | $20/mo or $200/yr | Unlimited | Unlimited |
| **Education** | $499 + $300/yr | Unlimited | Unlimited |
| **Enterprise** | $1K–5K/yr | Custom | Custom |

Community includes the full dashboard, cameras, scheduling, spool tracking, PWA, and keyboard shortcuts. Pro adds RBAC, SSO, orders/BOM, webhooks, analytics, white-labeling, MQTT republish, Prometheus metrics, and more.

License keys are verified locally — no phone home, no cloud dependency. Air-gap friendly.

→ [Get a Pro license](https://runsodin.com)

### Founders Program

We're offering 10 early adopters 90 days of free Pro access. Join the [Discord](https://discord.gg/odin-community) and ask for a Founders key.

---

## Documentation

| Document | Description |
|----------|-------------|
| [SECURITY.md](SECURITY.md) | Security policy and vulnerability reporting |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to contribute |
| [CHANGELOG.md](CHANGELOG.md) | Version history |
| [LICENSE](LICENSE) | BSL 1.1 (converts to Apache 2.0 on 2029-02-07) |
| `/api/docs` | Swagger API documentation (available on your running instance) |
| `/api/redoc` | ReDoc API documentation |

---

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ENCRYPTION_KEY` | Auto-generated | Fernet key for encrypting printer credentials |
| `JWT_SECRET_KEY` | Auto-generated | Secret for signing JWT tokens |
| `API_KEY` | Auto-generated | API key for endpoint authentication |
| `ODIN_HOST_IP` | Auto-detected | Host IP for WebRTC camera streaming |
| `DATABASE_URL` | `sqlite:////data/odin.db` | Database connection string |

On first run with Docker, secrets are auto-generated if not provided. They persist in `./odin-data/.env.generated`.

### Ports

| Port | Service | Protocol |
|------|---------|----------|
| 8000 | O.D.I.N. UI + API | HTTP |
| 1984 | go2rtc API | HTTP |
| 8555 | go2rtc WebRTC | TCP + UDP |

---

## Community

- **Discord**: [O.D.I.N. Community](https://discord.gg/odin-community)
- **GitHub Issues**: [Bug reports & feature requests](https://github.com/HughKantsime/runsodin/issues)
- **Website**: [runsodin.com](https://runsodin.com)
- **Support Email**: support@runsodin.com

---

## License

O.D.I.N. is licensed under the [Business Source License 1.1](LICENSE).

- **You can**: self-host, modify, and use for your own print farm operations
- **You cannot**: offer O.D.I.N. as a hosted service to third parties
- **On 2029-02-07**: the license automatically converts to Apache 2.0

This means the code is source-available today and will be fully open source in three years.

---

<div align="center">

**Built by [Sublab 3DP](https://runsodin.com)**

*O.D.I.N. — Because your print farm deserves better than a spreadsheet.*

</div>
