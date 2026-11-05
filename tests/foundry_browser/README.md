# Foundry web review (Parts A + B)

Use the existing dependency symlink; do not install packages or browsers:

```sh
cd frontend
npm run build
cd ..
ODIN_FOUNDRY_PYTHON=/path/to/odin-python-venv/bin/python node tests/foundry_browser/screenshots.mjs
```

Use a Python environment with ODIN's backend requirements, SQLAlchemy, cryptography
and pytest. Chromium must already be installed. The script uses the installed
`@playwright/test` and `@axe-core/playwright` packages.

Two loopback-only disposable apps use their own SQLite databases and ephemeral test
license: a seeded app and an empty first-run Setup app. Real cookies, real schemas
and real API reads; no changes to the Education fixture. No daemons run. Camera
transports and printer commands are blocked inside the fixture; camera enumeration's
go2rtc sync is disabled. Camera screenshots show unavailable transport honestly.

`seed.py` persists running, paused, ready, error, offline and unknown printers; AMS,
HT and ACE slots; loaded, feeding, empty, changed and support slots; spools; alerts;
scheduled/held jobs; chamber and H2D nozzle/plate facts. Part B adds a model, product
and component, order and line item, low stock consumable, project, completed print,
archive, failed recording, profile, detection, maintenance task/log, organization
and Education cost center. All are synthetic review records, never application
fallback values. There are no fabricated printable files, video bytes, completed
Education submissions or successful device actions. Blocked slots remain component
coverage because the backend has no reported flag. Media/provenance failure states
remain visible. Display observations and browser Date use the same review clock.

`routes.json` enumerates all **37 router entries**, including standalone pages,
redirects and the catch-all. The runner fails before launch if the router inventory
changes, and fails a capture if it lands on an unexpected path or lacks a Foundry
page boundary. Each route gets Classic and Foundry captures at **1440×900, 768×900,
390×900** (222 base captures). It also captures internal main-scroll positions,
Compact Floor/Printers, all eight additional Settings tabs, the reset-password form
(with an invalid token, never submitted), and the New Project modal (never saved).
Foundry routes and the project dialog get axe WCAG A/AA scans at every width.
Existing violations are recorded, never suppressed or reported as passes.

```sh
ODIN_FOUNDRY_PYTHON=/path/to/venv/bin/python node tests/foundry_browser/screenshots.mjs \
  --output=artifacts/foundry-part-b \
  --now=2026-11-04T12:00:00Z \
  --baseline=/path/to/2.0.42-capture
```

The report records each route, final URL, screenshots, axe violations, seed limits
and failures. Any failure yields a nonzero exit. Cleanup closes Chromium, terminates
both apps and removes temporary databases.

`--baseline` compares decoded Classic pixels, not PNG metadata. Missing captures
fail. Use the **same expanded fixture**, browser, fonts, time, locale and viewport
with a 2.0.42 frontend build to generate the baseline (`--classic-only` skips Foundry
for that run). Older Part A seed screenshots are not valid comparisons to this richer
seed. Without a baseline the report explicitly says pixel comparison was not run.
The source tree version is 2.0.41 despite the spec's reference to 2.0.42; no release
version was changed here.

`classic-baseline.json` contains source digests from Part A commit `3d372ba2`.
`RouteCoverage.test.tsx` checks them after undoing only explicitly guarded Foundry
adapters. This protects original handlers, queries, guards, classes, styles and
Classic markup. Primitive tests verify adapter rendering in Classic, light and
high contrast. These checks complement, and do not replace, the pixel comparison.

No Chromium was run during implementation. Browser layout, computed styles and axe
results remain pending the user's run. The fixture was checked using TestClient
login, 21 read routes, 16 Part B record types and the four existing board facts.
