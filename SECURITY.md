# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 1.0.x   | ✅ Active |
| < 1.0   | ❌ No     |

## Reporting a Vulnerability

If you discover a security vulnerability in O.D.I.N., **please report it responsibly**.

### How to report

Email **security@runsodin.com** with:

1. Description of the vulnerability
2. Steps to reproduce
3. Affected version(s)
4. Impact assessment (what an attacker could do)
5. Any suggested fix (optional but appreciated)

### What to expect

- **Acknowledgment** within 48 hours
- **Assessment** within 7 days
- **Fix or mitigation** within 30 days for critical/high severity
- **Credit** in the changelog and release notes (unless you prefer anonymity)

### Please do NOT

- Open a public GitHub issue for security vulnerabilities
- Share vulnerability details publicly before a fix is available
- Access, modify, or delete data belonging to other users during testing

## Security Architecture

O.D.I.N. is designed for self-hosted, air-gapped environments. Key security features:

### Authentication & Authorization
- JWT-based authentication with configurable secret keys
- Role-based access control (Viewer / Operator / Admin) enforced on all 164 API endpoints
- OIDC/SSO support for enterprise identity providers
- Rate limiting (10 login attempts per 5-minute window per IP)
- Account lockout after 5 failed attempts (15-minute cooldown)
- Password complexity enforcement (8+ chars, upper + lower + number)

### Data Protection
- Printer credentials encrypted at rest (Fernet symmetric encryption)
- Auto-generated secrets on first run (no hardcoded defaults in production)
- No telemetry, no analytics, no data leaves your network
- SQLite database with WAL mode for safe concurrent access

### Network Security
- All printer communication stays on your local network
- No cloud dependencies or phone-home behavior
- License verification is fully offline (Ed25519 signature validation)
- Camera streams proxied through go2rtc (credentials not exposed to browser)

### Audit Trail
- All login attempts logged with IP address and timestamp
- Administrative actions recorded in audit log
- Searchable audit log viewer (admin-only)

## Security Audit History

| Date | Version | Findings | Resolved |
|------|---------|----------|----------|
| 2026-02-09 | v1.0.0 | 53 findings (7 ship-blocking, 5 high, 12 medium, 8 low) | All 53 resolved |

### Ship-blocking fixes in v1.0.0
- RBAC enforcement on all mutating API endpoints
- JWT secret unification (OIDC + local auth)
- Setup endpoints locked after completion (SSRF prevention)
- Auth bypass fixes on label and branding endpoints
- SQL injection prevention via column whitelist on user updates
- Admin-only access on audit logs and backup downloads

## Hardening Recommendations

For production deployments:

1. **Use a reverse proxy** (Nginx, Caddy, Traefik) with TLS termination
2. **Set strong environment variables** — don't rely on auto-generated defaults for high-security environments
3. **Restrict network access** — O.D.I.N. only needs to reach your printers on the local network
4. **Back up regularly** — use the built-in backup feature in Settings → Data
5. **Keep updated** — `docker compose pull && docker compose up -d`
6. **Review audit logs** — check Settings → Audit Logs periodically

## Scope

This security policy covers the O.D.I.N. application code in this repository. It does not cover:

- Third-party dependencies (report those to their maintainers)
- Your network infrastructure or reverse proxy configuration
- Printer firmware vulnerabilities
- The go2rtc binary (report to [AlexxIT/go2rtc](https://github.com/AlexxIT/go2rtc))
