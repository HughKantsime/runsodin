# Foundry Part A screenshots

From the repository root, with the existing frontend dependencies linked:

```sh
cd frontend
npm run build
cd ..
ODIN_FOUNDRY_PYTHON=/path/to/odin-python-venv/bin/python node tests/foundry_browser/screenshots.mjs
```

Use a Python environment with ODIN's backend requirements, SQLAlchemy, cryptography,
and pytest (the canonical schema bootstrap uses the existing environment). The script
uses the already installed `@playwright/test` and `@axe-core/playwright` packages.
Chromium must already be installed. It does not install packages or browsers.

The script starts two loopback-only disposable apps with their own SQLite DBs and
an ephemeral test license: a rich seeded app and an empty first-run Setup app.
It uses real login cookies and real API reads. It does not change the Education
browser fixture. No daemons run. Printer commands and camera transport requests
are blocked inside the fixture, and camera enumeration's go2rtc sync is disabled.
Camera screenshots therefore show unavailable transport, not a pretend LIVE stream.

The separate `seed.py` includes running, paused, ready, error, offline and unknown
printers; AMS, HT and ACE slots; loaded/empty/changed/support slots; tracked spools;
real alert rows; a scheduled queue and an undated held job. Feeding and blocked slot
states are tested in Vitest only because the public API does not expose them.

All 37 route entries are captured in both themes at 1440×900, 768×900 and 390×900.
Desktop/tablet Floor and Printers also get Compact captures. Foundry's six main
routes receive axe WCAG A/AA checks at every width. The script keeps all failures in
`artifacts/foundry-part-a/report.json` and exits nonzero when any check fails.
It closes Chromium, terminates the apps and deletes the temporary DBs on exit.

Optional arguments:

```sh
ODIN_FOUNDRY_PYTHON=/path/to/venv/bin/python node tests/foundry_browser/screenshots.mjs \
  --output=artifacts/foundry-review \
  --now=2026-11-04T12:00:00Z \
  --baseline=/path/to/2.0.42-capture
```

`--baseline` compares decoded Classic pixels against files with identical names.
Use the same fixture, browser, fonts, review clock, locale, timezone and viewport
for the baseline. Missing baseline images fail the comparison. Without this option,
the report explicitly says Classic pixel comparison was not run. A baseline from
another seed or time is not valid evidence. The current source tree identifies
itself as **2.0.41**, despite the supplied spec referring to 2.0.42.

No Chromium run was performed during implementation. The Python fixture was checked
through real TestClient authentication and its seven main API reads, and the JS
runner passed `node --check`. Screen layout and browser axe results remain pending.
