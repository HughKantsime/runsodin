# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.17.x  | ✅ Current          |
| 0.16.x  | ✅ Supported        |
| 0.15.x  | ⚠️ Security fixes only |
| < 0.15  | ❌ Not supported    |

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

PrintFarm Scheduler is designed for deployment in controlled, air-gapped environments. Security is a core design constraint, not an afterthought.

### Authentication & Authorization

- **JWT-based authentication** with configurable expiry
- **API key authentication** for all non-public endpoints
- **Role-based access control (RBAC)**: admin, operator, viewer
- **bcrypt password hashing** with salt
- **OIDC/SSO support** (v0.17.0+) for Microsoft Entra ID integration
- **Visual permissions editor** for granular access control

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

### Notification Security (v0.17.0+)

- **Browser Push**: VAPID keys stored in database, not exposed to clients beyond public key
- **Email/SMTP**: Credentials stored in system_config table, supports STARTTLS
- **Webhooks**: URLs stored server-side, webhook secrets never exposed to frontend
- **All notifications**: Per-user opt-in preferences, no data sent without consent

### ITAR/CMMC Considerations

- Fully self-hosted — no cloud services required
- Air-gap ready — runs entirely on local network
- Audit logging for accountability
- Role-based access limits data exposure
- No data leaves the network boundary
- OIDC/SSO can be configured for GCC High environments

## Secrets Management

The following secrets should be unique per deployment and never committed to version control:

| Secret | Location | Purpose |
|--------|----------|---------|
| `API_KEY` | `backend/.env` | API authentication |
| `JWT_SECRET` | `backend/.env` | JWT token signing |
| `ENCRYPTION_KEY` | `backend/.env` | Fernet encryption for printer credentials |
| `VITE_API_KEY` | `frontend/.env` | Frontend API access |
| VAPID keys | Database `system_config` | Browser push notifications |
| SMTP password | Database `system_config` | Email notifications |
| OIDC client secret | Database `oidc_config` | SSO authentication |
| Webhook URLs | Database `webhooks` | Discord/Slack notifications |

### Generating Secrets

```bash
# Generate API_KEY
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# Generate JWT_SECRET
python3 -c "import secrets; print(secrets.token_urlsafe(64))"

# Generate ENCRYPTION_KEY (Fernet)
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Generate VAPID keys
python3 -c "from py_vapid import Vapid; v = Vapid(); v.generate_keys(); print(v.private_key, v.public_key)"
```

## Hardening Recommendations

For production deployments:

1. **Reverse proxy** — Put nginx/caddy in front with TLS termination
2. **Firewall** — Restrict access to the server's IP and port
3. **Unique secrets** — Generate unique `API_KEY`, `JWT_SECRET`, and `ENCRYPTION_KEY` values
4. **Database backups** — Use the built-in backup feature or schedule SQLite backups
5. **OS hardening** — Follow CIS benchmarks for your host OS
6. **Network segmentation** — Isolate the print farm VLAN from general network traffic
7. **OIDC/SSO** — Use enterprise SSO instead of local passwords for compliance environments
8. **Audit logs** — Review audit_logs table periodically for suspicious activity

## Files Excluded from Version Control

The following are gitignored and should never be committed:

- `backend/.env` — Contains all secrets
- `frontend/.env` — Contains API key
- `backend/printfarm.db` — Production database
- `backend/printfarm.db-shm` — SQLite WAL shared memory
- `backend/printfarm.db-wal` — SQLite WAL log
- `backend/backups/` — Database backup files
- `go2rtc/go2rtc.yaml` — Contains camera credentials
- `frontend/dist/` — Built frontend (contains baked-in env vars)
