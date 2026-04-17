# Conductor Track Registry

Active and completed development tracks.

## Active Tracks

| Track ID | Name | Type | Status | Step | Created |
|----------|------|------|--------|------|---------|
| telemetry-rewrite-bambu-first_20260417 | Printer Telemetry Rewrite — Bambu-First + Demo Engine | feature | new | PLAN | 2026-04-17 |
| agent-native-phase2-safety_20260415 | Agent-Native ODIN — Phase 2 Safety & Route Retrofit | feature | new | NOT_STARTED | 2026-04-15 |
| agent-native-odin_20260414 | Agent-Native ODIN — OpenClaw + MCP Integration | feature | new | PLAN | 2026-04-14 |

## Completed Tracks

| Track ID | Name | Completed | Summary |
|----------|------|-----------|---------|
| user-friction-and-fail-loud-gaps_20260414 | User Friction + Fail-Loud Gap Closure | 2026-04-14 | v1.8.8 shipped (PARTIAL promote). Dropped setup-token for WordPress-style first-user-wins. Added license/tier step to wizard + install-script public-exposure warning + vision dismiss-resume endpoint + /health/ready + digest delivery_status column + cron-status surface. New install-smoke CI workflow verified locally against ubuntu:24.04 before shipping. Docker image on GHCR; runsodin.com alias pending fix-forward on M4-runner → Vercel API connectivity. |
| kill-the-stubs-and-native-positioning_20260413 | Kill the Stubs + Native Positioning Refinement | 2026-04-13 | Shipped Spoolman push-back + automated quiet-hours digest delivery (no more "on roadmap" caveats). Split native positioning: iOS = mobile companion, macOS = desktop client. Cut v1.8.5. 24 new contract tests + 1 mid-flight architecture fix (cross-module imports). Live on runsodin.com via promote 24360868554. |
| codex-adversarial-remediation_20260412 | Codex Adversarial Remediation — All ODIN Repos | 2026-04-13 | Shipped in v1.8.2 → v1.8.4 across three codex verification passes. All 35 original findings + P1/P2 follow-ups closed across odin, odin-site, odin-native, odin-mcp. 33 contract tests added. Landmarks: DNS-pinned safe_post (R8), atomic complete_job (R5), DB-backed setup-token gate (R4), workflow monotonicity + persist-credentials=false (S4). |
| ws-token-mfa-hardening_20260221 | WebSocket Token Scoping + MFA Hardening | 2026-04-13 | Superseded — scope fulfilled. WebSocket auth tokens shipped in v1.3.59; MFA pending-token blacklisted on use in v1.3.63; TOTP MFA verified live under public-docs-alignment_20260413/T1.1. |
| public-docs-alignment_20260413 | Public Documentation Alignment | 2026-04-13 | Tightened Spoolman (pull-only), SSO (Microsoft Entra ID), quiet hours (suppression-only). Added Native Companion Apps section to runsodin.com + new odin-native README. TOTP verified real. 3 commits across all repos, live on production via promote 24355534056 |
| auth-missing-endpoints_20260221 | Auth — Missing Authentication on Endpoint Groups | 2026-02-22 | Shipped in v1.3.61 — all endpoints require require_role() |
| credential-encryption_20260221 | Credential Encryption — SMTP, MQTT, camera_url | 2026-02-22 | Shipped in v1.3.62 — Fernet encryption for SMTP/MQTT passwords and camera URLs |
| frontend-security_20260221 | Frontend Security Hardening | 2026-02-22 | Shipped in v1.3.64 — Three.js bundled, CSP wildcards removed, OIDC fixed, Google Fonts removed |
| path-traversal-sweep_20260221 | Path Traversal Sweep | 2026-02-22 | Shipped in v1.3.65 — realpath() boundary checks, upload size limits, label_class allowlist |
| e2e-security-coverage_20260221 | E2E Test Coverage — Security Hardening v1.3.57-59 | 2026-02-22 | 20 tests covering cookie auth, rate limiting, scope enforcement, SSRF, bounds, camera validation, last-admin, GDPR, audit logs |
| auth-hardening-architectural_20260221 | Auth Hardening — Architectural Items | 2026-02-21 | httpOnly cookie auth, go2rtc bind to 127.0.0.1, container non-root user, slowapi rate limiting, API token scope enforcement |
| dispatch-compatibility-guardrails_20260221 | Dispatch Compatibility Guardrails | 2026-02-21 | Bed-size + API-type guards on dispatch; gcode/3mf metadata extraction; printer bed config in UI; Models page badges; job modal warning |
| security-hardening-remaining_20260221 | Security Hardening — Remaining Quick Fixes | 2026-02-21 | JWT entropy, numeric bounds, camera URL validation, webhook SSRF, audit logs on login/password-change, GDPR export completeness |
| site-redesign_20260225 | Marketing Site Redesign + Docs Theme | 2026-02-25 | Full enterprise redesign: 19 components, Docusaurus amber theme, 8 commits, tsc-b clean, useReducedMotion on all animations |
| docs-buildout_20260225 | Docs Wiki Buildout — 21 Missing Pages | 2026-02-25 | 21 new wiki pages (40 total), 100% FEATURES.md coverage, sidebars wired, cross-links added, Docusaurus build clean |
| modular-architecture-refactor_20260226 | Modular Architecture Refactor | 2026-02-26 | 12 domain modules, app factory, event bus, ModuleRegistry DI, module-owned migrations, 171 contract tests; main.py 524→12 lines |
| route-splits_20260226 | Route File Splits — Sub-router Decomposition | 2026-02-26 | 8 oversized route files (7,450L) split into 25 sub-router files (max 595L); 8 commits, 1801 tests passing, 578 OpenAPI paths preserved |
| frontend-modular-refactor_20260226 | Frontend Modular Refactor — Domain Directory Structure | 2026-02-26 | api.js split into 15 domain modules (max 165L), 34 pages into 12 subdirs, 38 components into 10 subdirs; 113 files, 1801 tests passing |
| cross-module-violations_20260226 | Cross-Module Violation Cleanup | 2026-02-26 | 3 violations eliminated; OrgSettingsProvider via registry, calculate_job_cost to services.py, compute_printer_online deleted; KNOWN_VIOLATIONS allowlist removed; 209 contract tests pass |
| oversized-page-splits_20260226 | Oversized Page Splitting | 2026-02-26 | 5 page files (1,098-1,941L) each split under 400L; 19 extracted components/hooks, max component 537L; 1801 tests pass |
| large-backend-splits_20260226 | Large Backend File Splits | 2026-02-26 | 3 files (880-1,204L) split into 14 focused sub-modules (max 350L); event_dispatcher re-export shim; supervisord entry points unchanged; 1801 tests pass |
