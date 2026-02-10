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
│   ├── main.py           # FastAPI application (routes, models, logic)
│   ├── auth.py           # JWT authentication
│   ├── models.py         # SQLAlchemy models
│   ├── mqtt_monitor.py   # Bambu MQTT telemetry daemon
│   ├── moonraker_monitor.py  # Klipper telemetry daemon
│   └── ...
├── frontend/
│   ├── src/
│   │   ├── App.jsx       # Main layout and routing
│   │   ├── pages/        # Page components
│   │   ├── components/   # Shared components
│   │   └── api.js        # API client
│   └── ...
├── docker/
│   ├── supervisord.conf  # Process manager config
│   ├── entrypoint.sh     # Container startup script
│   └── go2rtc.yaml       # Camera streaming config
├── install/
│   └── docker-compose.yml  # User-facing install file
├── docker-compose.yml    # Dev compose (builds from source)
└── Dockerfile
```

### Making Changes

1. **Backend changes**: Edit files in `backend/`, then rebuild: `docker compose up -d --build`
2. **Frontend changes**: Edit files in `frontend/src/`, then rebuild. The frontend is built at Docker build time (no hot-reload in production mode).
3. **Test your changes**: Run through the UI manually. Check the browser console for errors. Check `docker logs odin` for backend errors.

## Code Style

- **Python**: Follow existing patterns in `main.py`. No strict linter enforced, but keep it clean.
- **React**: Functional components with hooks. TailwindCSS for styling. No CSS modules.
- **Commits**: Clear, descriptive messages. Reference issue numbers where applicable.

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
