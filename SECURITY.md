# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 1.0.x   | ✅ Current          |
| < 1.0   | ❌ Not supported    |

## Reporting a Vulnerability

If you discover a security vulnerability, please report it responsibly.

**Do not open a public GitHub issue for security vulnerabilities.**

Email: sublab3dp@gmail.com

Include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

You should receive a response within 48 hours.

## Security Architecture

O.D.I.N. is designed for deployment in controlled, air-gapped environments. Security is a core design constraint, not an afterthought.

### Authentication & Authorization
- **JWT-based authentication** with configurable expiry
- **Role-based access control (RBAC)** enforced on all 164 API endpoints — admin, operator, viewer
- **334 automated RBAC tests** verifying endpoint-level access control
- **bcrypt password hashing** with salt
- **OIDC/SSO support** for Microsoft Entra ID integration (GCC High compatible)
- **Visual permissions editor** for granular access control
- **Rate limiting** — 10 login attempts per 5-minute window per IP
- **Account lockout** — auto-lock after 5 failed attempts (15-minute cooldown)
- **Password complexity** — minimum 8 characters, requires uppercase + lowercase + number
- **Login audit trail** — all attempts logged with IP address

### Data Protection
- All data stored locally (SQLite with WAL mode) — no external database connections
- Printer credentials (access codes, API keys) encrypted at rest with Fernet
- No telemetry, analytics, or external API calls
- No external CDN dependencies (fonts, scripts, stylesheets)
- Database backup/restore functionality with admin-only access

### Network Security
- Designed for LAN-only deployment behind a firewall
- No outbound internet connections required (except optional: OIDC, SMTP, webhooks)
- MQTT communication stays within the local network
- Moonraker/Klipper REST polling stays within local network
- Static branding assets are the only unauthenticated routes (`/static/branding/*`, `/api/branding`)

### Notification Security
- **Browser Push**: VAPID keys stored in database, not exposed to clients beyond public key
- **Email/SMTP**: Credentials stored in system_config table, supports STARTTLS
- **Webhooks**: URLs stored server-side, webhook secrets never exposed to frontend
- **All notifications**: Per-user opt-in preferences, no data sent without consent

### License System
- **Ed25519 cryptographic signatures** — license keys are offline-verifiable signed tokens
- **Air-gap friendly** — no phone home, no cloud validation, no internet required
- **No license generator in repo** — the signing key is never distributed

### ITAR/CMMC Considerations
- Fully self-hosted — no cloud services required
- Air-gap ready — runs entirely on local network
- Audit logging for accountability
- Role-based access limits data exposure
- No data leaves the network boundary
- OIDC/SSO can be configured for GCC High environments

## Secrets Management

### Docker Deployment (Recommended)

On first run with `docker-compose up`, secrets are **auto-generated** and persisted to the `odin-data/` volume. No manual secret management is required for standard deployments.

| Secret | Location | Purpose |
|--------|----------|---------|
| `ENCRYPTION_KEY` | `odin-data/.env` (auto) | Fernet encryption for printer credentials |
| `JWT_SECRET_KEY` | `odin-data/.env` (auto) | JWT token signing |
| `API_KEY` | `odin-data/.env` (auto) | API authentication |
| VAPID keys | Database `system_config` | Browser push notifications |
| SMTP password | Database `system_config` | Email notifications |
| OIDC client secret | Database `oidc_config` | SSO authentication |
| Webhook URLs | Database `webhooks` | Discord/Slack notifications |

### Manual Secret Generation

If deploying without Docker or rotating secrets manually:

```bash
# Generate API_KEY
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# Generate JWT_SECRET
python3 -c "import secrets; print(secrets.token_urlsafe(64))"

# Generate ENCRYPTION_KEY (Fernet)
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Hardening Recommendations

For production deployments:

1. **Reverse proxy** — Put nginx/caddy in front with TLS termination
2. **Firewall** — Restrict access to the server's IP and port
3. **Unique secrets** — Verify auto-generated secrets exist in `odin-data/` (Docker handles this)
4. **Database backups** — Use the built-in backup feature or schedule SQLite backups
5. **OS hardening** — Follow CIS benchmarks for your host OS
6. **Network segmentation** — Isolate the print farm VLAN from general network traffic
7. **OIDC/SSO** — Use enterprise SSO instead of local passwords for compliance environments
8. **Audit logs** — Review audit_logs table periodically for suspicious activity

## Security Testing

O.D.I.N. includes automated security testing as part of its QA suite:

- **31 security-specific tests** covering auth bypass, injection, session management
- **334 RBAC tests** verifying all 164 API endpoints enforce proper role restrictions
- **53 security findings** identified and resolved during pre-release audit

To run the security test suite:

```bash
pip install -r tests/requirements-test.txt
pytest tests/test_security.py -v --tb=short
ADMIN_PASSWORD=<your-admin-password> pytest tests/test_rbac.py -v --tb=short
```

## Files Excluded from Version Control

The following are gitignored and should never be committed:

- `.env` — Contains all secrets
- `odin-data/` — Database, backups, secrets volume
- `backend/printfarm.db` — Production database
- `backend/printfarm.db-shm` — SQLite WAL shared memory
- `backend/printfarm.db-wal` — SQLite WAL log
- `backend/backups/` — Database backup files
- `go2rtc/go2rtc.yaml` — Contains camera credentials
- `frontend/dist/` — Built frontend (contains baked-in env vars)
