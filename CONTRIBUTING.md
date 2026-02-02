# Contributing to PrintFarm Scheduler

Thanks for your interest in contributing.

## Development Setup

```bash
# Clone the repo
git clone https://github.com/HughKantsime/printfarm-scheduler.git
cd printfarm-scheduler

# Backend
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # Edit with your settings
uvicorn main:app --reload

# Frontend (separate terminal)
cd frontend
npm install
npm run dev
```

The frontend runs on `http://localhost:3000` and proxies API calls to the backend on port 8000.

## Project Structure

```
printfarm-scheduler/
├── backend/
│   ├── main.py           # FastAPI app, all routes
│   ├── branding.py       # White-label branding models
│   ├── mqtt_monitor.py   # MQTT printer monitoring daemon
│   └── static/           # Uploaded branding assets
├── frontend/
│   ├── src/
│   │   ├── App.jsx       # Main layout, sidebar, routing
│   │   ├── pages/        # Page components
│   │   ├── permissions.js # RBAC config
│   │   └── BrandingContext.jsx
│   ├── vite.config.js
│   └── tailwind.config.js
├── go2rtc/               # Camera streaming config
├── docs/                 # Architecture diagrams
├── VERSION               # Current version (read at build time)
└── CHANGELOG.md
```

## Version Bumping

The version is defined in the root `VERSION` file and injected into the frontend at build time via Vite's `define` config. To bump the version:

1. Update `VERSION`
2. Add entry to `CHANGELOG.md`
3. Rebuild frontend: `cd frontend && npm run build`
4. Commit, tag, push

The login page and any other version references pick it up automatically.

## Code Style

- **Backend**: Python, FastAPI, SQLAlchemy. Single-file `main.py` (will be split as it grows).
- **Frontend**: React 18, Vite, TailwindCSS. Functional components with hooks.
- **Commits**: `v{X.Y.Z} - Short Description` for version bumps, descriptive messages otherwise.

## Branching

- `master` is the release branch
- Feature work can go on topic branches, but small fixes directly to master are fine for now

## Testing

No formal test suite yet. Test manually against a running instance. If you add tests, they're welcome.

## Scope

This project targets self-hosted, air-gapped environments. Contributions should not introduce external service dependencies (cloud APIs, CDNs, analytics, telemetry).
