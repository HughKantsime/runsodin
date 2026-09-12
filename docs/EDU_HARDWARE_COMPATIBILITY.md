# ODIN EDU Hardware Compatibility Evidence

The machine-readable matrix is `ops/edu_readiness/hardware-compatibility.json`. Fixture/parser tests are not a claim of physical-device certification.

| Family | Deterministic evidence | Live certification operation | Current EDU status |
|---|---|---|---|
| Bambu MQTT | Deep telemetry fixtures and state tests | TLS connect, subscribe to one redacted report topic, receive, disconnect; no publish or `pushall` | Real-device observation required |
| Moonraker / Klipper | Status fixture and parser test | GET-only exact allowlist; object query allowlist | Real-device observation required |
| PrusaLink | Status fixture and parser test | GET-only version/status/printer/job paths | Real-device observation required |
| Elegoo SDCP | Status fixture and parser test | Configured WebSocket receive/close only; UDP discovery disabled | Real-device observation required |

Certification mode must never upload/delete files, send G-code, start/pause/resume/stop a job, move axes, heat components, control lights, change settings, publish MQTT messages, send WebSocket frames, or discover by UDP. An unavailable device is `blocked`, not passed.
