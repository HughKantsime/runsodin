# ODIN EDU Readiness Guide

ODIN is EDU-ready only when the evidence-derived report says `READY`. Automated tests are an accessibility/security baseline, not a VPAT/ACR or legal certification.

## Candidate-image evidence

Run `make test-candidate` before treating a checked-out revision as a release candidate. The gate builds that revision's Dockerfile, verifies the running container uses the exact built image ID, claims and seeds a fresh disposable SQLite installation, and tests the real API, RBAC, WebSocket, compiled frontend, mobile layout, keyboard path, theme persistence, Community feature gates, and a synthetic `.3mf` upload. It rejects empty, skipped, expected-failure, failed, or errored JUnit results.

Evidence is written to `artifacts/candidate-gate/<run-id>/index.html`, with a redacted manifest, retained JUnit reports, sanitized container logs, and failure screenshots. A passing candidate gate proves deterministic software integration for that image; it does not prove Education license entitlement, external TLS, legal approval, backup operations on the deployment host, or physical printer/camera compatibility. Those remain separate EDU readiness rows and must not be inferred from this gate.

Before a school production deployment:

- All deterministic gates must pass: existing EDU foundation, compiled-browser and ASGI privacy lifecycles, backup/restore, four protocol contracts, API/WebSocket load thresholds, accessibility matrix/keyboard suite, security/dependencies, and artifact scanning.
- Verified HTTPS and certificate-expiry checks must pass for the actual hostname. Never bypass TLS verification.
- Each printer family the school will use must have a redacted, passive real-device observation.
- Run and record a backup/restore drill, available disk space, monitoring/alert routing, incident owner, support escalation, RPO/RTO, and recovery access.
- Complete institutional review of FERPA/COPPA/California student privacy duties, DPA/contract terms, retention/deletion, breach notification, record review/correction, accessibility procurement, and subprocessors.
- Use synthetic accounts and files in the POC. Issue a signed installation-bound Education license only for the approved sandbox host.
- Set `TRUSTED_HOSTS` to the sandbox/production DNS names (comma-separated), keep `CORS_ORIGINS` explicit, and retain secure/HttpOnly/SameSite session-cookie settings. Do not use the wildcard Host default for an EDU deployment.

Known blockers from the current engineering run are preserved in the generated HTML report rather than waived: expired production TLS, unavailable live legal-source re-attestation, physical-device observations, and legal/contract acceptance. The representative load gate and dependency audits are green after code/dependency remediation; backup/restore failure and rollback paths are covered by the deterministic suite but still require an operator drill on the deployment storage.

The browser privacy gate uses the compiled production frontend. It covers fresh login, reload rehydration, legacy-key cleanup in a new browser context, logout, session expiry, erasure, and post-erasure protected navigation. It verifies that identity/RBAC data is not persisted and that logout/expiry/erasure remove sensitive entries from local storage, session storage, Cache Storage, and IndexedDB while preserving non-sensitive preferences.

WebSockets require a short-lived purpose-limited token. Anonymous sockets, global API keys, and reuse of normal access tokens are rejected. Event delivery is filtered by explicit user audience or the referenced printer's organization; the load gate exchanges subscription and ping messages and performs 50 cross-user isolation checks in each repetition. The load harness uses pre-minted synthetic access/WebSocket tokens, so login and WebSocket-token issuance latency are not part of its measured profile.

The committed load mix is deterministically interleaved rather than executed as category-sized phases. Counts, concurrency, percentages, and thresholds remain fixed; interleaving represents simultaneous classroom reads, submissions, approvals, reporting, and session traffic without manufacturing an undocumented all-writes-at-once profile.

The deterministic security row verifies configured Host rejection, explicit CORS, secure session-cookie construction, `no-store` API/OpenAPI responses, protected OpenAPI and API-prefixed readiness aliases, public root liveness and database-backed root readiness, backup free-space refusal, and generic fail-closed error responses in addition to dependency, secret, SAST, and Dockerfile scans. Container/orchestrator checks use the exact public `/health/ready` route; other `/health/*` paths do not receive a broad authentication bypass.
