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
remain visible. Display observations, timeline running-job ends, reporting windows, SQLite seed
server defaults and browser Date use the same review clock. A fixed audit row is
seeded. Successful harness login audit rows are removed only in the disposable
fixture; real authentication, rate limits, sessions and cookies still run.

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

`--baseline` compares decoded Classic pixels, not PNG metadata. Each RGBA channel
may differ by at most **10**; any unmasked pixel beyond that fails. Strictly failing comparisons emit
`*-diff.png`, `changedPixels`, dimensions and a `boundingBox` in `classicDiffs`.
A missing or extra Classic capture also fails.

Round 3 adds one explicit selector mask in `comparison_masks.mjs`:
`button[aria-label^="Notifications,"] > span[aria-hidden="true"].rounded-full`
(the alert bell count badge only). Raw PNGs remain unchanged. Every screenshot
manifest entry records `comparisonMasks`, with the selector, id and each visible
viewport rectangle `{x,y,width,height}` in pixels (outward rounding, no padding).
Empty rectangle lists are recorded when no badge is visible. `maskDefinitions`
lists the complete allowlist; `comparisonMasks` at report root records actual and
baseline rectangles for each Classic comparison, including passes. Both inputs
exclude those exact pixels in the comparison. Missing measurements, changed
selectors, or moved/resized/missing badges fail rather than widening a mask.
Dimension changes still fail even inside a mask. All other pixels retain the
10-channel tolerance and the optional volatility rule below. There is no automatic
masking. **Recapture both baselines with this harness**: old reports have no DOM
mask measurements and cannot be compared. No baseline app changes are required.
 Optionally pass
`--volatile=/path/to/base2/report.json`, the **base-vs-base** comparison report at
the same tolerance and viewport sizes. Only filenames in its `classicDiffs` get a
noise allowance: **all** changed pixels must fit inside that reference bounding
box padded by **4px** on each edge, and the count must be **at most twice** the
reference count. Dimensions must match. Other captures retain the strict rule.
Accepted noise is recorded separately in `volatilePasses` (including the measured
box/count and allowed box/count); failures remain in `classicDiffs`. Diff PNGs
are retained for both. Do not use a branch-vs-base report for calibration. Copy the **whole**
`tests/foundry_browser/` directory, including `pixel_compare.mjs`, `layout_checks.mjs`, and `comparison_masks.mjs`, to the base
worktree and build its unchanged frontend. No app changes or dependencies are
needed in the current `odin-foundry-base` worktree.

Navigation waits for `load`, rendered route content and fonts, followed by 600ms.
Mounted Recharts charts get another 2200ms to finish their 1500ms animation; this
also works with the unmodified baseline. Camera pages keep the transport-failure
settle. Captures use the viewport plus main-content scroll parts (document scroll
parts on standalone pages), so internal scrollers cannot produce blank full-page
padding. Settings, compact variants and the dialog are also recorded in the
screenshot manifest. The Projects opener is named “New”; its dialog is “New Project”.

Run the base twice with identical arguments and compare run two to run one, then
compare the branch to run one with `--volatile` pointing to run two’s report. Keep output directories separate. The fixture now seeds `base_url=https://odin-review.example.test`, avoiding the
random server port in Settings > Notifications. Rebuild both baseline runs using
this fixture before comparing the branch. A new baseline
using this harness is required; older full-page captures are incompatible.

`--baseline` requires the baseline's `report.json` as well as its PNGs. Missing captures
fail. Use the **same expanded fixture**, browser, fonts, time, locale and viewport
with a 2.0.42 frontend build to generate the baseline (`--classic-only` skips Foundry
for that run). Older Part A seed screenshots are not valid comparisons to this richer
seed. Without a baseline the report explicitly says pixel comparison was not run.
The source tree version is 2.0.41 despite the spec's reference to 2.0.42; no release
version was changed here.

`classic-baseline.json` contains source digests from Part A commit `3d372ba2`.
`RouteCoverage.test.tsx` checks them after undoing only explicitly guarded Foundry
adapters. This protects original handlers, queries, guards, classes, styles and
Classic markup. Narrow normalization exceptions cover nonvisual a11y attributes
and the explicitly restored Dashboard footer: the Part A source digest already
contained that regression, so the restored header/footer pair is checked by the
Dashboard regression test against pre-Foundry markup instead. Primitive tests verify adapter rendering in Classic, light and
high contrast. These checks complement, and do not replace, the pixel comparison.

No Chromium was run during implementation. Browser layout, computed styles and axe
results remain pending the user's run. The fixture was checked using TestClient
login, 24 read routes, 16 Part B record types and the four existing board facts.

Layout checks run for every Foundry capture, including scroll parts, Settings tabs,
compact views and the project dialog. At 768/390, `rightOverflow` records displayed
elements inside `[data-foundry-page]` that exceed the viewport right edge (1px
rounding allowance), even if a clipping ancestor hides the excess. Content inside
an in-viewport horizontal auto/scroll container is exempt; the scroller itself is
not. Subtrees marked `inert`, `aria-hidden="true"`, or `visibility: hidden` are
skipped (including descendants that override visibility). Open drawers still count. Native checkboxes are measured together with their associated clickable
labels for the 44px target check. The cheap `wrappedWords` check uses Range text
rectangles only for visible, single-word leaf buttons/pills/badges. Mixed markup
and multi-word labels are excluded to avoid unreliable line counts.

Pure harness regressions (no browser or backend):

```sh
node --test tests/foundry_browser/*.test.mjs
```
