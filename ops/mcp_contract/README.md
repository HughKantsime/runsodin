# Disposable MCP live contracts

From this ODIN checkout, with existing backend and package dependencies:

```sh
ops/mcp_contract/run_live.sh /path/to/odin-mcp-221
```

No installs, Docker, production credentials, or printer hardware are used. The
runner uses Python 3.11 and the checked-out backend, not a released image. The
package currently checks for ODIN **2.2.0**. Run outside a sandbox that prohibits
binding localhost sockets. Exit codes: the package suite's code, 2 for setup or
configuration refusal, 130/143 for interruption/termination.

Each run owns a private temporary directory, the application's fully bootstrapped
SQLite database, an OS-assigned API port bound to `127.0.0.1`, and the existing
`StatefulHardwareHttpPeer` Moonraker simulator in
`ops/hardware_certification/simulators.py`. The simulator begins in `printing`,
accepts real adapter pause/resume requests, and never writes the ODIN database.
Only the API writes the recorded printer state. No monitor or scheduler is started.

Production's device destination policy intentionally blocks loopback printers. The
harness uses the existing test-only exemption in
`tests/support/loopback_device_policy/sitecustomize.py`, then narrows device
resolution to the **exact simulator host and port**. Other ports, hosts, and aliases
are refused. The real Moonraker adapter, ITAR checks, DNS pinning, and command
responses are retained. This test seam does not change production code or policy.

`runtime.py` creates the real application, including routes, module registration,
authentication, scope checks, feature gates, and idempotency middleware. It replaces
only the application's lifespan so notification subscribers, dispatch workers,
camera synchronization, and other background writers are not started. A new
database has no outbound notification destinations; submitted fixtures have no
submitter to notify. Python's socket audit fence additionally refuses connections,
DNS resolution, and binds outside literal `127.0.0.1`. The package's live suite
restricts fetch to the configured origin and refuses redirects.

The environment is cleared before importing backend configuration, and the working
directory is the private directory, so no existing credentials, proxies, integration
settings, or `.env` files are loaded. Any inherited nonempty variable ending in
`DATABASE_URL` is refused unless it exactly names this run's database. The npm
process receives only its run-specific environment, has offline mode enabled, and
uses a temporary npm cache. The generated token is read into that process's
environment only; it is never printed or passed on the command line. The shell trap
stops the owned runner and removes its directory on success, failure, or signal;
the runner stops npm's process group, API, and simulator.

## Fixtures and code findings

`seed_fixture.py --out EMPTY_DIRECTORY --printer-port PORT` creates every ID in the
package's `test/live/README.md`: a model, simulated printer and slot 1, four separate
failed/pending/submitted/submitted jobs, unread undismissed user alert, active
unassigned PLA spool and library entry, overdue maintenance task, and pending order.
All requested states are supported directly by the application models; no alternate
states or schema migration were needed. The maintenance template is overdue because
the printer's 20-hour service counter exceeds its 10-hour interval. Job approval is
enabled in `SystemConfig`; the admin still creates pending jobs under the real policy.
The seeder refuses nonempty output directories, including already-created databases.
It writes `fixtures.json` and `token`, both mode 0600, inside a mode 0700 directory.

The admin is created through the capacity-checked user service; login and token
minting use real routes with an in-process TestClient. There is no fixture SQL.
Maintenance and job approval use a one-day Pro license signed by a fresh test key.
Only this harness process trusts its public key, as in the existing `tier_license`
test fixture. No vendor key, real license, or online activation is used. Standalone
seeding does not enable that test license in a separately launched ordinary server;
use `run_live.sh` for the complete harness.

The README's `agent:write`-only credential is insufficient for the package's version
preflight on this release. A real request to `/api/v1/version` returned **403**:

```json
{"detail":"Insufficient token scope — 'read' scope required"}
```

The fixture therefore mints `agent:write` **and** `read`, without modifying product
authorization or the package. Normal API startup has no universal background-off
switch, hence the test-only lifespan above. The live suite uses the checked-out
backend per Slice 2, despite older package documentation referring to a released image.

## Verification

```sh
python3.11 -m pytest tests/mcp_contract/test_harness.py --confcutdir=tests/mcp_contract -p no:cacheprovider -q
```

The conftest boundary is intentional: `tests/conftest.py` loads `.env.test` and tries
to restart an existing server. These tests never load it. They bootstrap a real
database, verify every fixture and authentication, exercise the real read routes
and queue preview, and check database refusal, network fencing, exit propagation,
and cleanup on termination. They do not claim to replace the package's live suite.

For the same isolation when running repository guards:

```sh
PYTEST_ADDOPTS='--confcutdir=tests/test_contracts' /path/to/odin/.claude/tools/guard-tests.sh "$PWD"
```

The local build sandbox refused `bind(('127.0.0.1', 0))` with `PermissionError`
(errno 1). The runner reports this clearly and exits 2. The full package live suite
therefore still needs an ODIN Dev/CI run; no live tool pass or failure count is claimed.
No code here is copied into the product image by the Dockerfile.
