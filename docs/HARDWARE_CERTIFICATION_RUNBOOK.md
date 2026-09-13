# ODIN Hardware Certification Operator Runbook

## Safety and claim boundary

Run replay freely; it binds only loopback simulators. Do not run `observe` or `exercise` until the target file has been reviewed for the intended lab printer. `observe` is receive/GET-only. `exercise` mutates a real printer and requires a cleared physical area, a disposable job, an emergency-stop-ready operator, and a one-time exact-action authorization.

No physical-device evidence has been captured in this repository. All four EDU live-hardware rows remain blocked.

## 1. Code-controlled replay

```sh
make test-hardware-certification
```

Use the printed `artifacts/hardware-certification/<run-id>/index.html`. The report must say `SIMULATED REPLAY ONLY`; it cannot be imported as live evidence.

## 2. Protected target file

Store one JSON file per printer outside the repository and evidence directory. It must be owned by the invoking user, mode 0400 or 0600, nonsymlink, and inside non-group/world-writable parent directories. Use a private, loopback, or link-local address, or a hostname resolving to exactly one such address.

Example Bambu shape with fictional values:

Generate a protected, random correlation key once per target. Reuse it only when an observe artifact and an exercise artifact must be proven to concern the same target; rotate it to intentionally break that relationship. It is not a serial number or hardware identifier, and only its domain-separated SHA-256 is retained in evidence.

```sh
openssl rand -hex 32
```

```json
{"schema_version":1,"protocol":"bambu","target_alias":"lab-1","model_family":"X1","evidence_correlation_key":"REPLACE_WITH_64_LOWERCASE_HEX_CHARACTERS","connection":{"host":"10.0.0.10","port":8883,"ftps_port":990,"device_token":"REPLACE","access_code":"REPLACE"}}
```

Moonraker accepts `host`, `port`, optional `api_key`, and optional `tls`. PrusaLink requires either `api_key` or `username` plus `password`; TLS requires an API key. Elegoo accepts `host`, `port`, and optional `mainboard_id` (required for active commands).

## 3. Passive live observation

```sh
python3.11 -m ops.hardware_certification observe \
  --target-config /secure/operator/path/target.json
```

Review the HTML report. A valid run records two parsed samples, normalized capabilities/version data, commit/dirty state, strict assertion counts, hashes, and a sanitation pass. It does not retain endpoints, credentials, hardware IDs, topics, payloads, filenames, or student/user data.

Model identity must come from the device protocol, never from the configured expectation. Bambu reports `printer_type`. Elegoo reports `Attributes.MachineName` on a separate unsolicited `sdcp/attributes/<device>` frame; ODIN correlates that device suffix with two `sdcp/status/` or `sdcp/notice/` samples and also reads `FirmwareVersion` and `ProtocolVersion` from the attribute frame. Moonraker's documented `/server/info` response and PrusaLink's documented allowlisted responses do not expose printer model identity. PrusaLink `/api/version.printer` is a software-version string, not a model; its documented firmware field is `/api/version.firmware`. Moonraker and PrusaLink passive evidence therefore currently fail closed with `model_identity_unavailable`; a configured `model_family` is never substituted as proof. Failed observations likewise retain `model_family: unknown`.

Before import, bind verification to the model and the firmware/API versions recorded during the observation. Verification always derives the current ODIN `HEAD` itself and rejects evidence from any other commit or from a dirty source tree; there is no caller override. Any commit or device-identity mismatch expires the evidence and requires a new passive observation:

```sh
python3.11 -m ops.hardware_certification verify-artifact \
  /secure/operator/path/bambu-observe --mode observe --protocol bambu \
  --model-family X1C --firmware-version 01.08.00.00 --api-version unknown
```

## 4. Default-deny active authorization

For upload-capable workflows, place the disposable test asset outside the repository and evidence tree, set mode 0600, and use only `.3mf` (Bambu) or `.gcode` (Moonraker and PrusaLink). PrusaLink `.bgcode` is rejected because the certification path does not yet preserve and prove that format end to end.

```sh
python3.11 -m ops.hardware_certification authorize-template \
  --target-config /secure/operator/path/target.json \
  --actions upload,start,pause,resume,stop \
  --asset /secure/operator/path/disposable.3mf \
  --output /secure/operator/path/authorization.json
```

For Elegoo, omit `--asset` and add `--elegoo-filename` with the exact manually loaded job name. The template retains only a salted filename hash.

The generated template is intentionally unusable. Within 15 minutes, the named operator must set `authorization_state` to `AUTHORIZED`, copy `challenge` exactly into `operator_confirmation`, set all three safety acknowledgements to `true`, and approve every requested action individually. Do not add actions or fields.

## 5. Authorized exercise

```sh
python3.11 -m ops.hardware_certification exercise \
  --target-config /secure/operator/path/target.json \
  --authorization /secure/operator/path/authorization.json \
  --ledger /secure/operator/path/used-nonces \
  --asset /secure/operator/path/disposable.3mf
```

Authorization is validated and atomically consumed before a network backend is created. The worker re-observes state and exact same-run job identity before and after each command, then stops on the first mismatch. If setup, execution, cleanup, or an interrupt fails after consumption, a sanitized failure artifact is published before control returns to the caller. The nonce ledger stays outside the artifact tree. PrusaLink exposes only atomic `upload_start`; Elegoo cannot upload or start.

## 6. EDU evidence import

Place one verified observe artifact under each protocol name. Create a separate protected mode-0600 identity file outside the evidence tree; every imported protocol requires all three current values, using the literal `unknown` only when that protocol cannot expose the value:

```json
{"schema_version":1,"protocols":{"bambu":{"model_family":"X1C","firmware_version":"01.08.00.00","api_version":"unknown"}}}
```

Then run:

```sh
make verify-edu-live \
  HARDWARE_EVIDENCE_DIR=/secure/operator/path/verified-hardware \
  HARDWARE_IDENTITY_FILE=/secure/operator/path/current-identities.json
```

Replay or exercise artifacts do not populate live rows. Evidence must be clean, fresh, hash-valid, from the exact current commit and implementation, and must match the protected current model/firmware/API expectation during import. The import fails if either the evidence directory or identity file is supplied alone. Age, commit, implementation, model, firmware, or API drift makes evidence `BLOCKED` pending recapture.

## 7. Telemetry V2 handoff

After verified Bambu observe and exercise artifacts exist:

```sh
python3.11 -m ops.hardware_certification telemetry-prerequisites \
  --observe-artifact /secure/operator/path/bambu-observe \
  --exercise-artifact /secure/operator/path/bambu-exercise \
  --output /secure/operator/path/telemetry_v2_prerequisites.json
```

This maps only same-target, same-model proven live status, commands, and dispatch rows. An active `ams_read` proves only that the command path returned data; it does not prove ODIN's Telemetry V2 AMS slot synchronization, so the AMS row remains blocked until that application path is observed directly. Database/alert transitions and the seven-day staging soak also remain blocked. This command does not change `ODIN_TELEMETRY_V2`, deploy anything, or remove the legacy path.
