# Education verification coverage

Updated October 9, 2026. Source baseline: v1.9.23, d22d6627927c859b7dd4d28cd6cb2d3012263ddb. New connected tests are local pending commit/publication. A passing release gate does not certify every workflow or a physical printer.

## Evidence meanings

- **Connected local evidence:** actual services, persistent database and real transport clients against disposable loopback peers. This validates integration behavior within the tested protocol simulation.
- **Component evidence:** a service, policy, parser, UI or adapter test with some boundaries replaced. Necessary but insufficient to certify the full workflow.
- **Live school evidence:** school version/configuration, firmware/network and physical print observed. Never infer it from either local category.

## Coverage matrix

| Area | Current evidence | Gap or required confirmation |
| --- | --- | --- |
| General application HTTP and desktop/mobile UI | v1.9.23 CV04: 4 API scenarios and 9 actual browser scenarios, including persona permissions, navigation, upload, settings and seeded data | Community-mode smoke; not the full Education pilot |
| Google/OIDC access | Existing identity/tenant contracts and prior callback browser regression | Actual school authentication/config remains external; login alone does not certify printing |
| Class roster, grants and approved printers | Admin/review/tenant contracts | Full actual browser setup and multi-class teacher/student journey still need coverage |
| Storage and upload parsing | Upload/storage/readiness contracts; real parsed archive in connected workflow | Genuine school Orca archive and actual configured volume/quota; concurrent unrelated writers |
| Teacher approval and scheduler | Real parser -> approval -> scheduler in connected tests; scheduler API permission/repeat-run contracts | New browser gate covers actual student multipart upload, teacher compatibility/approval, operator HTTP scheduling and visible scheduled state; Jobs UI scheduling click remains separate |
| Pre-dispatch safety | Connected wrong-printer, revoked entitlement, changed material and tampered-file denials; real DB policy, no transport opened | Concurrency/races have separate component contracts, not real printer observations |
| Bambu file transfer | Both paths exercise real implicit FTPS, synthetic authentication, PROT P, encrypted data and byte-identical payload | Firmware TLS session reuse, passive-port/VLAN reachability and school-specific reset stage |
| Transport failure and retry | Control reset, final-response reset, PROT rejection, data TLS failure; persisted scheduled state, reservation removed, explicit healthy retry | Additional firmware-specific failure behavior; reset cause not established |
| Print command | Both paths publish through authenticated TLS MQTT to real local broker; exact opaque filename, plate, AMS and options asserted | Broker receipt is not firmware acceptance, a printer acknowledgment or physical start |
| Duplicate actions | Duplicate dispatch cannot reach adapter; single start command; duplicate terminal observation does not transition | Lost printer acknowledgment and process restart while action in flight need deeper connected evidence |
| Job completion | Real dispatch confirmation and monitor policy persist completion; unknown token/wrong printer rejected | New broker-to-monitor checks cover both telemetry paths, reconnect and completion/failure/cancellation. Whole-process crash recovery and out-of-order cross-job packets remain separate gaps |
| UI messages and progress | Existing frontend contracts and generic browser smoke; privacy browser uses stubbed API responses | New actual browser gate covers upload errors, approval, rejection reason, refreshed scheduling and same-class student isolation. Dispatch error UI and full lifecycle UI need hardware rehearsal |
| Upgrade, backup and restart | v1.9.23 CV05 SQLite/PostgreSQL parity and CV07 disposable sandbox; internal backup/readiness verified | Interrupted dispatch/retry after application restart and full pilot after upgrade |
| Other printers and generic workflows | Existing contracts, generic candidate smoke and replay fixtures | Not live Moonraker/PrusaLink/Elegoo certification; this connected gate is Bambu-specific |
| Physical hardware certification | v1.9.23 CV08 manifest explicitly says `mode: replay` | Replay is not live certification; school transfer/start/completion still unverified |

## Runnable connected gate

```sh
bash ops/education_pilot/check-local.sh
python3.11 -m pytest tests/test_contracts/ -q --tb=short
```

The existing CV02 contract gate collects these tests automatically. No alternate scheduler, paid service or competing release pipeline is introduced. The test host needs the existing Python dependencies, OpenSSL, mosquitto and mosquitto_passwd. Missing dependencies fail the test; they do not silently skip it. All credentials/files are synthetic; sockets bind loopback and database/broker/certificates are disposable.

## Checks required before claiming the Monday pilot ready

1. Actual Education HTTP/browser journey with student, teacher and operator, including the distinction between approval, scheduling and dispatch. Verify visible status and useful failures.
2. Broker-to-monitor ingestion and subscriber reconnect are now exercised for both paths. Still require whole-process restart, late/out-of-order observations and uncertain firmware command acknowledgment in the pilot.
3. School-version/model/firmware and redacted working curl comparison to establish where their reset occurs. No transport guess promoted as a verified cause.
4. Genuine sliced Orca file and school firmware/network transfer -> accepted command -> observed physical print. If unavailable, report blocked rather than certify it.
5. Fix any reproduced failures, review affected changes once, and run appropriate existing candidate/release gates. Preserve the full passing evidence and report partial/blocked scenarios explicitly.

The obsolete v1.6.2 API guardrail fixture with an audit-role token is not an acceptable current-release baseline. Do not weaken application permissions to make it pass.


## October 9 isolated pilot preparation
Two actual browser scenarios use a fresh compiled frontend, real login/JWT routes, multipart upload, teacher review, operator HTTP scheduling, same-class student privacy, malformed archive errors and teacher rejection reason. No API response stubs. Ephemeral signed test entitlement exists only in a disposable subprocess; production app lifespan is disabled to exclude unrelated camera/background tasks. This does not validate the shipped container startup or real issuer activation.

Six connected monitor cases use authenticated TLS MQTT reports through the actual legacy/V2 adapter, production monitor, database provider and lifecycle. Subscriber reconstruction resumes the same observation; completion/failure/cancellation and duplicate packets do not issue a second start command. This is subscriber reconnect, not whole-process crash recovery, and retained peer packets are simulated firmware.

A normal published-image real-printer pilot is prepared in pilot-compose.yml and real-printer-pilot.md. It is separate from ops/edu_sandbox's intentionally isolated replay lifecycle. Host/printer selection, valid internal Education entitlement and physical rehearsal remain pending. No remote instance created or production changed.


## Session reuse regression, v1.9.24 candidate
The shared production helper now offers the control TLS session to the protected data connection. The strict TLS 1.2 positive test uses the production method unmodified and checks actual server session_reused plus identical payload. Only the negative baseline intentionally omits reuse. Connected classroom success, healthy retry and monitor paths use the strict peer. This verifies the local protocol requirement, not firmware acceptance or strict TLS 1.3 resumption. The v1.9.23 diagnostic failure remains historical evidence, not the candidate's expected behavior.
