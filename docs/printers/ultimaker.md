# Ultimaker S-series and Ultimaker 3 (Experimental, monitoring only)

O.D.I.N. can monitor an Ultimaker S-series printer or an Ultimaker 3 over the network: state, temperatures, job progress and the camera.

**Support level: Experimental, monitoring only for now.** This support is built from Ultimaker's public local API documentation. It has **not been verified on real hardware**. Sending jobs and controlling prints (pause, resume, stop) are **not available for this printer type yet**, and the scheduler never assigns work to an Ultimaker printer. O.D.I.N. only reads from the printer; it sends no command of any kind.

Not covered: Cura Connect and Digital Factory (cloud), the material station, firmware updates, and the Ultimaker 2 (it has no network API).

## Set up

1. Make sure the printer is on the same network as O.D.I.N. and note its IP address. The printer shows its network address in its network settings (on the printer's screen). Alternatively look it up in your router's device list.
2. In your browser, open `http://<printer address>/docs/api/`. If the printer's API documentation page opens, the address is right.
3. In O.D.I.N., add a printer and choose **Ultimaker — Experimental**.
4. Enter the address. There is no password or key field: reading needs no credentials.
5. Use **Test connection**. It reads `GET /api/v1/system` and shows the printer's own variant (for example "Ultimaker S5") and firmware.

The Ultimaker monitor reads its printer list when it starts. Restart the O.D.I.N. container after you add an Ultimaker printer, change its address, or deactivate it. (When the license fleet lock is on, the monitor starts new printers by itself and restarts to pick up changed or removed ones.)

## What O.D.I.N. does

| What | Ultimaker API | Notes |
|---|---|---|
| Identify the printer | `GET /api/v1/system` | variant and firmware, used by Test connection and logged by the monitor |
| Read state and temperatures | `GET /api/v1/printer` | bed, and the first extruder as the nozzle |
| Read the job | `GET /api/v1/print_job` | HTTP 404 means "no job" |

The monitor polls every 10 seconds. Only HTTP GET is ever sent, with a 5 second timeout and a 64 KiB limit on each answer.

State mapping (fail closed):

- Printing: job state `pre_print`, `printing`, `post_print` or `resuming`.
- Paused: `pausing`, `paused` or `wait_user_action`.
- Idle: no job (404) or job state `none`; `wait_cleanup` is a finished job waiting for the plate to be cleared.
- Error: the printer reports `error`.
- Unknown: anything else, including the printer states `maintenance` and `booting`, an unrecognised state string, a malformed or oversized answer, or two answers that contradict each other (for example the printer says printing but there is no job). Unknown is never shown as idle or printing, does not refresh "last seen", and never starts or ends a print.
- Offline: after 3 polls in a row get no answer. The printer goes back to its real state at the first answer.

Progress is read as 0 to 1 and shown as a percentage. Remaining time is the printer's total time minus the elapsed time; it is left empty when the printer gives no total. For a dual-extruder printer only the first extruder is shown as the nozzle temperature.

A print is recorded as completed only when the printer reports the finished state (`wait_cleanup`) at 100 %. If the job ends any other way, including a job that disappears between two polls, it is recorded as cancelled rather than guessed to have succeeded. A new job id seen while the previous print was running ends the previous one as cancelled and starts the new one.

## Camera

When the printer is added with its camera enabled and no camera URL is set, O.D.I.N. sets the camera URL to the documented default, `http://<printer address>:8080/?action=stream` (the printer's MJPEG stream). It goes through the same private-address checks as every other camera URL, and it is not probed: if your printer has no camera or the stream is on another port, clear or change the camera URL by hand on the printer's page.

## What is verified

- Verified: request and answer handling against recorded exchanges built from the API documentation (every documented state, the 404 "no job" answer, malformed, oversized and non-JSON answers, two extruders, timeouts), that only GET is sent, that the pinned device request and the ITAR check are used, and the monitor's start, progress, end, offline and recovery handling.
- Not verified: behaviour on a real Ultimaker. Every endpoint, field name and state string comes from the documentation. If something does not work as expected, send printer diagnostics.

## If something is wrong

Run the guided printer test (Troubleshooting, printer test) for the printer and send the report to support, together with the printer's variant and firmware version and what the O.D.I.N. printer page shows. The report checks that the saved address is reachable and that `/api/v1/system` answers.

## Coming later

Pairing (a person confirms on the printer's screen), pause, resume and stop, and job sending are planned as separate steps, each with their own review. Until then they are refused.
