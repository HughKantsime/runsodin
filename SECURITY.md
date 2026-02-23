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
- httpOnly session cookie auth (XSS-resistant); Bearer token and X-API-Key fallbacks for API clients
- Role-based access control (Viewer / Operator / Admin) enforced on all 200+ API endpoints — auth expectations documented in a 1507-test RBAC matrix with CI coverage gate
- OIDC/SSO support for enterprise identity providers (redirect_uri pinning, secure code exchange)
- Rate limiting: 10 failed logins per 5-minute window per IP (account-level); global slowapi limiter (10 req/min auth, 30 req/min upload)
- Account lockout after 5 failed attempts (15-minute cooldown)
- Password complexity enforcement (8+ chars, upper + lower + number)
- Password change immediately revokes all existing sessions
- MFA (TOTP) pending tokens blacklisted on use — prevents session duplication
- Scoped API tokens (read/write/admin scopes) with per-route enforcement
- Container runs as non-root `odin` user; go2rtc HLS/API bound to 127.0.0.1 (no direct external access)

### Data Protection
- Fernet encryption at rest for: printer API keys, SMTP passwords, MQTT republish passwords, camera URLs with embedded credentials, Discord/Slack/Telegram/ntfy webhook URLs, org-level webhook settings, OIDC auth codes
- Camera RTSP credentials never persisted to DB — generated on-demand from the encrypted API key
- API keys stripped from all printer API responses
- Auto-generated secrets on first run (no hardcoded defaults in production); API_KEY startup warning if unset
- No telemetry, no analytics, no data leaves your network
- SQLite database with WAL mode for safe concurrent access
- Docker base images pinned to SHA256 digests; go2rtc binary SHA256-verified at build time

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
| 2026-02-21 | v1.3.57–1.3.59 | Exposure audit: api_key in responses, camera URL credential leak, SSRF, XXE, ZIP bomb, missing auth guards, JWT entropy, numeric field injection, httpOnly cookie migration, go2rtc network isolation, non-root container, rate limiting | All resolved |
| 2026-02-21 | v1.3.61–1.3.67 | Authorization sweep: ~30 endpoints missing require_role(), 2 IDOR bugs (users export, groups detail), credential plaintext at rest (SMTP/MQTT/webhook/OIDC), OIDC redirect injection, frontend supply chain (CDN Three.js, VITE_API_KEY footgun), 8 path traversal issues, Docker supply chain | All resolved |
| 2026-02-23 | v1.3.69 | RBAC matrix coverage: ~120 routes with undocumented auth expectations; 5 incorrect auth expectations (including IDOR cases); CI pipeline gaps | All resolved |

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
