# ODIN EDU Hardware Compatibility Evidence

The machine-readable matrix is `ops/edu_readiness/hardware-compatibility.json`. A replay pass proves ODIN's code-controlled transport and parser contracts; it is not physical-device certification and cannot satisfy an EDU live-hardware row.

| Family | Replay | Passive observation | Authorized exercise | Physical evidence |
|---|---|---|---|---|
| Bambu | TLS MQTT report peer + Telemetry V2 | Two report messages, no publish | Upload/start/pause/resume/stop; AMS read is transport-only | Blocked—none recorded |
| Moonraker / Klipper | Exact GET transcript + shared parser | Two object-status snapshots; live model identity is fail-closed because the documented API does not expose it | Upload/start/pause/resume/cancel | Blocked—none recorded |
| PrusaLink | Exact GET transcript + shared parser | Two status/job snapshots; live model identity is fail-closed because `/api/version.printer` is a software version, not a model | Atomic upload-start, pause/resume/stop | Blocked—none recorded |
| Elegoo SDCP | Separate unsolicited attributes frame plus two status frames + shared parser | Receive-only attributes/status/notice frames correlated by device-topic suffix | Pause/resume/stop of preauthorized job | Blocked—none recorded |

`observe` and `exercise` are separate commands and processes. Observe has no import path to application command adapters, file upload, discovery, camera, or raw G-code. Exercise consumes an expiring mode-0600 authorization before constructing any connection-capable backend, stops on the first state or identity mismatch, and can operate only on a same-run disposable job (or the one salted filename preauthorized for Elegoo).

Physical evidence expires after 30 days and on commit changes. Missing hardware is `blocked`, never skipped or passed. See `docs/HARDWARE_CERTIFICATION_RUNBOOK.html` for the operator workflow.

Passing live observation requires device-reported model identity. ODIN never substitutes the configured model when observation fails. Bambu reports its model in telemetry, while Elegoo reports it on a separate unsolicited attributes topic that ODIN correlates with status frames. Current Moonraker and PrusaLink evidence truthfully returns `model_identity_unavailable` until a documented printer-model source is available.

Observe and exercise artifacts can be combined only when their protected target files use the same random `evidence_correlation_key` and report the same model family. Evidence retains only a domain-separated hash of that key. Telemetry V2 AMS synchronization remains blocked until the application slot-sync path—not merely the active `ams_read` transport action—is observed on real hardware.
