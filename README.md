# PrintFarm Scheduler

A self-hosted, ITAR/CMMC-compliant job scheduler and monitoring system for 3D print farms. Built for environments where data can't leave the network.

![Version](https://img.shields.io/badge/version-0.9.0-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Python](https://img.shields.io/badge/python-3.12-blue)
![React](https://img.shields.io/badge/react-18-blue)

## What It Does

PrintFarm Scheduler manages a fleet of 3D printers from a single dashboard. It tracks what filament is loaded where, schedules jobs to minimize filament changes, monitors printers via MQTT in real time, and streams live camera feeds — all without touching the cloud.

### Key Features

**Scheduling & Jobs**
- Smart job scheduling with color-match scoring to minimize filament swaps
- Timeline view (Gantt-style) for visualizing printer schedules
- Priority queue with drag-and-drop job management
- .3mf file upload with automatic color/filament extraction
- Job states: pending → scheduled → printing → completed/failed

**Printer Management**
- Real-time MQTT monitoring (temperatures, print progress, status)
- Bambu Lab printer integration (X1C, P1S, A1, H2D) with AMS filament detection
- Anycubic Kobra support
- Automatic filament slot tracking from AMS RFID data
- Encrypted credential storage (Fernet) for printer access codes

**Camera Feeds**
- Live camera streaming via go2rtc (RTSP → WebRTC)
- Auto-detected from Bambu printer credentials — no manual URL config needed
- Cameras page with configurable grid layout (1/2/3 columns)
- Modal overlay for quick-look from any page
- All streams proxied through backend with authentication

**Filament & Spool Tracking**
- RFID spool identification via Bambu AMS
- Local filament library with color matching
- Spoolman integration (optional, for existing inventory systems)
- Color name resolver for Bambu's named colors (e.g., "Caramel Matte" → hex)

**User Management & Security**
- JWT authentication with role-based access control (admin/operator/viewer)
- API key authentication for all endpoints
- Admin dashboard for user management
- Audit logging
- All data stays on your network — no external dependencies required

**Analytics & Reporting**
- Printer utilization stats
- Revenue tracking and value-per-hour calculations
- Job history and completion rates
- Per-printer performance metrics

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                        Web Frontend                              │
│                    React 18 + Vite + TailwindCSS                 │
│  Dashboard │ Timeline │ Jobs │ Printers │ Cameras │ Analytics    │
└────────────────────────────┬─────────────────────────────────────┘
                             │ /api (REST)
┌────────────────────────────▼─────────────────────────────────────┐
│                      FastAPI Backend                             │
│  ┌──────────┐ ┌───────────┐ ┌──────────┐ ┌────────────────────┐ │
│  │ Scheduler│ │ MQTT      │ │ Auth     │ │ Camera Proxy       │ │
│  │ Engine   │ │ Monitor   │ │ (JWT)    │ │ (WebRTC signaling) │ │
│  └──────────┘ └───────────┘ └──────────┘ └────────────────────┘ │
└───────┬──────────────┬──────────────┬──────────────┬─────────────┘
        │              │              │              │
   ┌────▼────┐  ┌──────▼──────┐ ┌────▼────┐  ┌─────▼──────┐
   │ SQLite  │  │ Bambu MQTT  │ │Spoolman │  │  go2rtc    │
   │   DB    │  │ (printers)  │ │  (opt)  │  │ RTSP→WebRTC│
   └─────────┘  └─────────────┘ └─────────┘  └────────────┘
```

## Supported Printers

| Printer | Status | Features |
|---------|--------|----------|
| Bambu Lab X1C | ✅ Full | MQTT, AMS/RFID, camera ✅, file send |
| Bambu Lab P1S | ✅ Full | MQTT, AMS/RFID (no camera hardware) |
| Bambu Lab A1 | ✅ Full | MQTT, AMS/RFID (no LAN Live View) |
| Bambu Lab H2D | ✅ Full | MQTT, camera ✅ |
| Anycubic Kobra S1 | 🔄 Basic | Manual tracking |

## Quick Start

### Prerequisites

- Ubuntu 22.04+ (or similar Linux)
- Python 3.12+
- Node.js 18+
- go2rtc (for camera feeds)

### Installation

```bash
git clone https://github.com/HughKantsime/printfarm-scheduler.git
cd printfarm-scheduler

# Backend
cd backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # Edit with your settings

# Frontend
cd ../frontend
npm install
npm run build

# Start
cd ../backend
uvicorn main:app --host 0.0.0.0 --port 8000
```

### Camera Setup (Optional)

```bash
# Install go2rtc
wget https://github.com/AlexxIT/go2rtc/releases/latest/download/go2rtc_linux_amd64
chmod +x go2rtc_linux_amd64
mv go2rtc_linux_amd64 /usr/local/bin/go2rtc

# Config is auto-generated from printer credentials
# Enable LAN Live View on each Bambu printer's LCD:
# Settings → Network → LAN Live View → Enable
```

## Configuration

Environment variables (`.env`):

| Variable | Description | Default |
|----------|-------------|---------|
| `DATABASE_URL` | SQLite database path | `sqlite:///./printfarm.db` |
| `API_KEY` | API key for authentication | Required |
| `JWT_SECRET` | Secret for JWT tokens | Required |
| `SPOOLMAN_URL` | Spoolman instance URL | `None` (optional) |
| `BLACKOUT_START` | No-print window start (HH:MM) | `22:30` |
| `BLACKOUT_END` | No-print window end (HH:MM) | `05:30` |
| `ENCRYPTION_KEY` | Fernet key for credential encryption | Auto-generated |

## ITAR/CMMC Compliance

PrintFarm Scheduler is designed for controlled environments:

- **Fully self-hosted** — no cloud services, no external API calls
- **Air-gap ready** — runs entirely on your local network
- **No telemetry** — zero data leaves your infrastructure
- **Local filament matching** — no external databases for spool identification
- **Encrypted at rest** — printer credentials stored with Fernet encryption
- **Role-based access** — admin/operator/viewer permission levels
- **Audit logging** — track who did what and when

## API

Interactive API docs available at `http://your-server:8000/docs` when running.

Core endpoints:

| Endpoint | Description |
|----------|-------------|
| `GET /api/printers` | List all printers with filament state |
| `POST /api/jobs` | Create a new print job |
| `POST /api/scheduler/run` | Run the scheduling engine |
| `GET /api/timeline` | Get scheduled timeline |
| `GET /api/cameras` | List available camera feeds |
| `GET /api/analytics` | Dashboard analytics data |
| `POST /api/print-files/upload` | Upload .3mf files |

## Roadmap

- [x] Smart job scheduling with color matching
- [x] Timeline/Gantt view
- [x] Bambu Lab MQTT integration
- [x] AMS RFID spool tracking
- [x] .3mf upload with color extraction
- [x] User authentication (JWT + API key)
- [x] Role-based access control
- [x] Camera feeds via go2rtc (validated on X1C & H2D)
- [x] Analytics dashboard
- [x] Collapsible sidebar UI
- [x] Upload → Model auto-creation
- [ ] Send .3mf directly to printer
- [ ] Auto-deduct filament on job complete
- [ ] Schedule from Models page
- [ ] SSL/TLS (nginx reverse proxy)
- [ ] Mobile responsive layout
- [ ] Database backups
- [ ] Sortable columns on Jobs page
- [ ] AI print failure detection via camera
- [ ] Multi-site federation
- [ ] Enterprise edition (white-label, SSO, multi-tenant)

## License

MIT
