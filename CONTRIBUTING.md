# Contributing to O.D.I.N.

Thanks for your interest in contributing.

## Development Setup

```bash
# Clone the repo
git clone https://github.com/HughKantsime/runsodin.git
cd runsodin

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
runsodin/
├── backend/
│   ├── main.py              # FastAPI app, all routes
│   ├── models.py            # SQLAlchemy models
│   ├── branding.py          # White-label branding models
│   ├── mqtt_monitor.py      # MQTT printer monitoring daemon (Bambu)
│   ├── moonraker_monitor.py # Klipper/Moonraker monitoring daemon
│   ├── prusalink_monitor.py # PrusaLink monitoring daemon
│   ├── elegoo_monitor.py    # Elegoo SDCP monitoring daemon
│   ├── printer_events.py    # Universal printer abstraction
│   ├── license_manager.py   # Ed25519 license verification
│   └── static/              # Uploaded branding assets
├── frontend/
│   ├── src/
│   │   ├── App.jsx          # Main layout, sidebar, routing
│   │   ├── pages/           # Page components
│   │   ├── permissions.js   # RBAC config
│   │   ├── i18n/            # Translations (EN, DE, JA, ES)
│   │   └── BrandingContext.jsx
│   ├── vite.config.js
│   └── tailwind.config.js
├── tests/
│   ├── test_rbac.py         # 863 RBAC endpoint tests
│   ├── test_security.py     # 31 security tests
│   ├── test_features.py     # 62 feature tests
│   ├── test_e2e/            # 75 Playwright E2E tests
│   └── README.md            # Test suite documentation
├── go2rtc/                  # Camera streaming config
├── docker/                  # Docker entrypoint and configs
├── systemd/                 # Service files for bare-metal installs
├── docs/                    # Architecture diagrams
├── VERSION                  # Current version (read at build time)
└── CHANGELOG.md
```

## Version Bumping

The version is defined in the root `VERSION` file and injected into the frontend at build time via Vite's `define` config.

To bump the version:
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

- `master` is the release branch — always deployable
- `dev` is the working branch — all development happens here
- Feature work goes on topic branches off `dev`
- Merge to `master` only for tagged releases

## Testing

O.D.I.N. has a comprehensive QA suite with 1,031 automated tests across 5 phases.

```bash
# Install test dependencies
pip install -r tests/requirements-test.txt

# Run all tests
ADMIN_PASSWORD=<your-admin-password> pytest tests/ -v --tb=short

# Run specific phases
ADMIN_PASSWORD=<your-admin-password> pytest tests/test_rbac.py -v      # RBAC (863 tests)
pytest tests/test_security.py -v                                        # Security (31 tests)
pytest tests/test_features.py -v                                        # Features (62 tests)
ADMIN_PASSWORD=<your-admin-password> pytest tests/test_e2e/ -v          # E2E (75 tests)
```

If you add or change API endpoints, please verify RBAC coverage with the test suite. New features should include test coverage where practical.

## Scope

This project targets self-hosted, air-gapped environments. Contributions should not introduce external service dependencies (cloud APIs, CDNs, analytics, telemetry).
