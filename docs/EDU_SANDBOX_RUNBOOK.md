# ODIN Education Sandbox Runbook

This lifecycle creates an isolated, fictional proof-of-concept. It does not deploy to production, contact the license service, mint a license, or connect to real printers.

## Safety model

- Every sandbox ID must be a 3–48 character lowercase slug.
- Every Docker resource is unique and label-owned. ODIN, the broker, and the simulator attach only to an internal network with no outbound route. A read-only, capability-dropped TCP proxy with no secret/data mounts is the sole edge member and binds the web UI to a dynamic `127.0.0.1` port; MQTT is never published.
- The checked-out Dockerfile is built once. The application and Bambu replay publisher must run that exact image ID.
- Credentials are random 0600 files and a named secrets volume. Values do not enter Compose environment metadata, command arguments, state, logs, JSON, or HTML.
- `ACTIVE` requires a normally signed, unexpired, installation-bound Education license. There is no sandbox bypass or test signing key.
- Reset, expiry, certification, and purge require the exact sandbox ID as acknowledgement.

## 1. Prepare

Start from a clean checkout:

```sh
make edu-sandbox-prepare EDU_SANDBOX_ID=ctec-poc
make edu-sandbox-status EDU_SANDBOX_ID=ctec-poc
```

Preparation builds the exact candidate, generates installation/device identity, verifies readiness and image identity, and stops the application. The public receipt is `.odin-edu-sandboxes/ctec-poc/public/activation-receipt.json`. No Education features are claimed yet.

If preparation was interrupted in `PREPARING` (or its prepare-failure `DEGRADED` state), retry only through the bounded recovery path. It verifies names/labels, removes only matching partial resources, and then rebuilds:

```sh
python3.11 -m ops.edu_sandbox prepare ctec-poc --recover
```

## 2. Create the proof-of-possession handoff

On an authorized, internet-connected licensing workstation, obtain a single-use nonce for the prepared installation. Use hidden terminal prompts so neither value enters shell arguments/history or environment variables; the helper sends one bounded JSON document to the lifecycle command over non-TTY stdin:

```sh
python3.11 - <<'PY'
import getpass, json, subprocess
request = {
    "key": getpass.getpass("ODIN license key: "),
    "nonce": getpass.getpass("Single-use issuer nonce: "),
}
subprocess.run(
    ["make", "edu-sandbox-request-license", "EDU_SANDBOX_ID=ctec-poc"],
    input=json.dumps(request).encode(),
    check=True,
)
PY
```

The secret request is `.odin-edu-sandboxes/ctec-poc/secrets/activation-request.json` (0600). Deliver it to the existing issuer out of band. Public artifacts contain only its SHA-256 digest. Do not email the license key, nonce, request, or signed artifact unless the approved secure delivery process explicitly permits it.

## 3. Activate

After the issuer returns a signed artifact for the exact installation, pipe it over stdin. The sandbox lease must be UTC and cannot outlive the license:

```sh
make edu-sandbox-activate \
  EDU_SANDBOX_ID=ctec-poc \
  EDU_EXPIRES_AT=2026-10-15T23:59:59Z < signed-odin-license.txt
```

Activation independently verifies signature, Education tier, explicit binding, installation match, expiry, and digest inside the exact candidate. It then verifies the application reports the same facts, proves that only the hardened proxy has an edge route, checks all personas/RBAC/tenant data, and starts only the synthetic Bambu replay.

Credentials remain in `.odin-edu-sandboxes/ctec-poc/secrets/`. Transfer them to school staff only through an approved password manager or encrypted channel. Never attach the directory to a ticket or email.

## 4. Operate and reset

```sh
make edu-sandbox-status EDU_SANDBOX_ID=ctec-poc
make edu-sandbox-reset EDU_SANDBOX_ID=ctec-poc
```

Reset deletes mutable database/uploads only after stopping the sandbox. It preserves and re-verifies installation ID, device-key fingerprint, signed-license digest, personas, school graph, exact images, and replay heartbeat. An interrupted reset becomes `DEGRADED`; recover only after reviewing status:

```sh
python3 -m ops.edu_sandbox reset ctec-poc --confirm ctec-poc --recover
```

## 5. Expire and reconcile

```sh
make edu-sandbox-expire EDU_SANDBOX_ID=ctec-poc
make edu-sandbox-reconcile EDU_SANDBOX_ID=ctec-poc
```

Expiry changes the local lease, not the signed license. Reconciliation stops the application and publisher, verifies both stopped, and preserves data during the operator’s grace period. `ops/edu_sandbox/reconcile.example.sh` is a one-shot scheduler example; this track does not install cron or launchd.

## 6. Certify or purge

Full certification is destructive: it checks readiness, resets, expires, reconciles, and purges.

```sh
make edu-sandbox-certify EDU_SANDBOX_ID=ctec-poc
```

Without a signed license, certification reports `BLOCKED_EXTERNAL`, never `PASS`. For direct cleanup:

```sh
make edu-sandbox-purge EDU_SANDBOX_ID=ctec-poc
```

Purge removes only exact label-owned containers, volumes, both networks, the unique candidate tag, and controller files. It verifies the prior loopback port is closed and that no license mount or heartbeat volume remains. Sanitized JSON and clickable HTML tombstones remain under `.odin-edu-sandboxes/.tombstones/`; HTML/JSON/JUnit certification evidence is under `artifacts/edu-sandbox/`. A purged sandbox ID is terminal and cannot be reused.

## Failure handling

- `PREPARING` failure: inspect state, then purge or use `prepare --recover`; recovery verifies and removes only label-owned partial resources.
- `REQUESTING_LICENSE` failure: temporary secret input is removed and state returns to `PREPARED`.
- `ACTIVATING` failure from `PREPARED`: the staged license is removed, services are stopped, and state returns to `PREPARED` only after cleanup proof.
- Failed renewal from `EXPIRED`: the prior bound license and expired state are restored; failed restoration becomes `DEGRADED`.
- `DEGRADED` reset: use `--recover` only when identity/license/resource invariants still match; otherwise purge.
- Foreign/missing labels, symlinks, state/resource mismatch, stale heartbeat, wrong image, wrong role, or residual resources are hard failures.

Never send a school IT contact a report marked `FAIL`, and describe `BLOCKED_EXTERNAL` as “awaiting signed license evidence,” not “EDU ready.”
