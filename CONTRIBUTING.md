# Contributing to O.D.I.N.

Thanks for your interest in contributing to O.D.I.N.! Here's how to get involved.

## Ways to Contribute

### Report Bugs
Open a [GitHub issue](https://github.com/HughKantsime/runsodin/issues) with:
- What you expected to happen
- What actually happened
- Steps to reproduce
- Your environment (OS, Docker version, browser)
- Screenshots if applicable

### Request Features
Open an issue tagged `feature-request`. Describe the problem you're trying to solve, not just the solution you want. The best feature requests explain the "why."

### Join the Community
- [Discord](https://discord.gg/odin-community) — chat, help others, share your setup
- Star the repo — it helps with visibility

### Submit Code
We welcome pull requests for bug fixes and small improvements. For larger changes, open an issue first to discuss the approach.

## Development Setup

```bash
git clone https://github.com/HughKantsime/runsodin.git
cd runsodin
docker compose up -d --build
```

The app will be available at `http://localhost:8000`.

### Project Structure

```
runsodin/
├── backend/
│   ├── main.py           # FastAPI app shell — wires routers, middleware, static files
│   ├── routers/          # 13 router modules (printers, jobs, users, vision, orders, etc.)
│   ├── deps.py           # Shared FastAPI dependencies (auth, RBAC, org scoping)
│   ├── models.py         # SQLAlchemy ORM models
│   ├── schemas.py        # Pydantic request/response schemas
│   ├── auth.py           # JWT authentication helpers
│   ├── mqtt_monitor.py   # Bambu MQTT telemetry daemon
│   ├── moonraker_monitor.py  # Klipper/Moonraker WebSocket daemon
│   ├── prusalink_monitor.py  # PrusaLink REST polling daemon
│   ├── elegoo_monitor.py     # Elegoo SDCP WebSocket daemon
│   ├── vision_monitor.py     # Vigil AI inference daemon
│   ├── timelapse_capture.py  # Timelapse frame capture daemon
│   ├── report_runner.py      # Scheduled report execution daemon
│   └── ...
├── frontend/
│   ├── src/
│   │   ├── App.jsx       # Main layout and routing
│   │   ├── pages/        # Page components
│   │   ├── components/   # Shared components
│   │   └── api.js        # Centralized API client (all pages use fetchAPI)
│   └── ...
├── docker/
│   ├── supervisord.conf  # Process manager config (9 managed processes)
│   ├── entrypoint.sh     # Container startup script + DB migrations
│   └── go2rtc.yaml       # Camera streaming config
├── tests/
│   ├── test_features.py  # Integration tests
│   ├── test_rbac.py      # RBAC auth expectations matrix (1507 tests)
│   ├── test_route_coverage.py  # Ensures all routes have RBAC coverage
│   └── test_security.py  # Security-specific tests
├── install/
│   └── docker-compose.yml  # User-facing install file
├── docker-compose.yml    # Dev compose (builds from source)
└── Dockerfile
```

### Making Changes

1. **Backend changes**: Edit files in `backend/`, then rebuild: `docker compose up -d --build`
2. **Frontend changes**: Edit files in `frontend/src/`, then rebuild. The frontend is built at Docker build time (no hot-reload in production mode).
3. **Test your changes**: `make test` runs the full test suite (features + RBAC); `make test-coverage` runs the route coverage gate.
4. **Security scan**: `make scan` runs bandit (Python static analysis), pip-audit (CVEs), and npm audit locally.
5. **Logs**: `make logs` tails the container output. `make shell` opens a bash shell inside the container.

## Code Style

- **Python**: Follow existing patterns in `backend/routers/`. CI runs bandit static analysis — high-severity findings block merge. Run `make scan` before submitting a PR.
- **React**: Functional components with hooks. TailwindCSS for styling. No CSS modules. Use `fetchAPI` from `api.js` for all API calls — do not create local fetch wrappers.
- **New endpoints**: Add auth expectations to `ENDPOINT_MATRIX` in `tests/test_rbac.py`. The route coverage gate (`make test-coverage`) will fail CI if you don't.
- **Commits**: Clear, descriptive messages using conventional commit format (`feat:`, `fix:`, `security:`, `chore:`). Reference issue numbers where applicable.

## Pull Request Process

1. Fork the repo and create a branch from `master`
2. Make your changes
3. Test locally with Docker
4. Submit a PR with a clear description of what and why
5. Wait for review

## Security Vulnerabilities

**Do NOT open public issues for security vulnerabilities.** Email security@runsodin.com instead. See [SECURITY.md](SECURITY.md).

## License

By contributing, you agree that your contributions will be licensed under the same [BSL 1.1](LICENSE) license as the project. Contributions will become Apache 2.0 licensed when the BSL converts on 2029-02-07.
