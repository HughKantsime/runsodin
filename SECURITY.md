# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.9.x   | ✅ Current          |
| < 0.9   | ❌ Not supported    |

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
- JWT-based authentication with configurable expiry
- API key authentication for all non-public endpoints
- Role-based access control: admin, operator, viewer
- bcrypt password hashing with salt

### Data Protection
- All data stored locally (SQLite) — no external database connections
- Printer credentials (access codes, API keys) encrypted at rest with Fernet
- No telemetry, analytics, or external API calls
- No external CDN dependencies (fonts, scripts, stylesheets)

### Network Security
- Designed for LAN-only deployment behind a firewall
- No outbound internet connections required
- MQTT communication stays within the local network
- Static branding assets are the only unauthenticated routes (`/static/branding/*`, `/api/branding`)

### ITAR/CMMC Considerations
- Fully self-hosted — no cloud services
- Air-gap ready — runs entirely on local network
- Audit logging for accountability
- Role-based access limits data exposure
- No data leaves the network boundary

## Hardening Recommendations

For production deployments:

1. **Reverse proxy** — Put nginx/caddy in front with TLS termination
2. **Firewall** — Restrict access to the server's IP and port
3. **Unique secrets** — Generate unique `API_KEY`, `JWT_SECRET`, and `ENCRYPTION_KEY` values
4. **Database backups** — Schedule regular SQLite backups to a secure location
5. **OS hardening** — Follow CIS benchmarks for your host OS
6. **Network segmentation** — Isolate the print farm VLAN from general network traffic
