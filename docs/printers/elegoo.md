# Elegoo (SDCP)

O.D.I.N. monitors Elegoo printers that speak SDCP (Centauri Carbon and similar FDM printers) and controls running prints. Sending jobs is **Experimental**.

**Support level.** Monitoring, pause, resume, print speed and stop are Supported and work as before. Job sending is Experimental: it is built from Elegoo's public SDCP documentation and has not been verified on real hardware. It stays off until a platform administrator turns it on for that printer (`PUT /api/printers/{id}/experimental-actions`, audited). The existing controls never need the opt-in. The printer card does not yet show an Experimental badge or toggle for Elegoo; use the API.

## Set up

1. Add the printer as type Elegoo with its address. The port is 3030 unless you add one (`192.168.1.50:3030`).
2. Enter the printer's MainboardID in **API Key** (the address finder fills it in). SDCP needs it for every command; without it nothing is sent.
3. To send jobs from O.D.I.N., a platform administrator turns on the opt-in for that printer. Restart the container after changing an Elegoo printer's address or MainboardID so the monitor reads the new list.

## What O.D.I.N. does

Source tags: **repo** = already used by O.D.I.N.'s Elegoo monitor before job sending; **docs** = SDCP public documentation, not verified on hardware.

| Action | Protocol | Source | Succeeds only when |
|---|---|---|---|
| Monitor | WebSocket `ws://host:3030/websocket`, topic `sdcp/status/<MainboardID>` | repo | status frames arrive |
| Pause, stop | Cmd 129, Cmd 130 | repo | the frame is written (no answer is read) |
| Resume, speed | Cmd 131, Cmd 403 (`PrintSpeedPct`) | repo | the frame is written (as before; no opt-in). Speed has no screen or route yet |
| Admission check | Cmd 0, then a `sdcp/status/<MainboardID>` frame | repo | the configured board reports idle |
| Send a file | `POST http://host:3030/uploadFile/upload`, multipart, 1 MiB chunks (`Check`, `Offset`, `Uuid`, `TotalSize`, `S-File-MD5`, `File`) | docs | every chunk: HTTP 200 and `{"code": "000000", "success": true}` |
| Start the print | Cmd 128 (`Filename`, `StartLayer`, `Calibration_switch`, `PrintPlatformType`, `Tlp_Switch`) on `sdcp/request/<MainboardID>`; answer on `sdcp/response/<MainboardID>` matched by `RequestID` and `Cmd` | id: repo; fields and answer: docs | `Data.Ack` is 0 |

Every HTTP and WebSocket connection goes through the pinned device destination policy (validated address, no redirects, no proxies, ITAR check for uploads). Only `.gcode` files are sent.

## Outcomes

- A non-zero `Ack` or a non-success upload answer is a refusal. Nothing started.
- No answer after the upload or the start command (timeout, connection lost, malformed or unmatched reply) is an unknown outcome, not a refusal. O.D.I.N. does not send again; check the printer before retrying.
- A start that is not confirmed, including a refusal, is handled like every other printer type: the job needs reconciliation before it is retried.

## What is verified

- Verified: request and response handling against scripted exchanges built from the documentation (upload success, refusal and timeout, start accepted, refused and unanswered, pause, resume, stop), the opt-in checks, explicit routing, and the admission status read.
- Not verified on a real printer: the upload endpoint, its form fields, chunk size and success body; the start command's fields and the meaning of each `Ack` value; whether the printer wants a bare file name or a path in `Filename`; whether a finished print leaves the printer in a state the admission check accepts as idle (it accepts only idle).

## Limits

- G-code (`.gcode`) only. Resin printers (Saturn) are not a target for job sending.
- Pause, resume, stop and speed do not wait for the printer's answer (unchanged from before). Only the start command does.
- A print started from O.D.I.N. is seen by the monitor as a start; whether the monitor attaches it to the dispatched job has not been verified.
- If something does not work as expected, send printer diagnostics.
