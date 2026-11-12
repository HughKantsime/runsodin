# OctoPrint (Experimental)

O.D.I.N. can monitor an OctoPrint server and, once a platform administrator allows it for that printer, send jobs to it and control prints.

**Support level: Experimental.** The OctoPrint support is built from OctoPrint's public REST API documentation (docs.octoprint.org). It has not been verified on real hardware. Monitoring is always on. Sending jobs and resume stay off until a platform administrator turns them on for that printer. Pause and stop always work.

## Set up

1. In OctoPrint, create an application key: open **Settings → Application Keys** (or use your user's API key under **Settings → API**). Use a key for a user who may control the printer and read settings (settings are how O.D.I.N. finds the camera).
2. In O.D.I.N., add a printer and choose **OctoPrint — Experimental**.
3. Enter the OctoPrint address. If OctoPrint is not on port 80, add the port (for example `192.168.1.40:5000`). A plain OctoPi install usually answers on port 80.
4. Paste the application key into **API Key**, then use **Test connection**.
5. To send jobs or resume from O.D.I.N., a platform administrator turns on **Experimental actions** for that printer. Read the warning first.

After you add the first OctoPrint printer, restart the O.D.I.N. container once so the OctoPrint monitor process starts with it.

## What O.D.I.N. does

| Action | OctoPrint API | Succeeds only when |
|---|---|---|
| Identify the server | `GET /api/version` | HTTP 200 with JSON |
| Read state and temperatures | `GET /api/printer`, `GET /api/job` | HTTP 200 with JSON |
| Send a job and start it | `POST /api/files/local` (`file`, `select=true`, `print=true`) | HTTP 201 and `done: true` |
| Pause, resume, stop | `POST /api/job` | HTTP 204 |
| Find the camera | `GET /api/settings` (`webcam.streamUrl`) | an absolute `http(s)` URL |

State is read from OctoPrint's flags and fails closed: printing, pausing and cancelling show as printing; paused as paused; error or closed-on-error as error; operational and ready as idle. HTTP 409 ("printer not operational") shows as offline. Anything else, including a malformed answer, shows as unknown and never as idle or printing. An offline or unknown printer does not refresh its "last seen" time and never starts or ends a print.

## What is verified

- Verified: the request and response handling against recorded exchanges built from the API documentation (status variants, upload 201/400/409/415, control 204/409, malformed JSON, timeouts), the state mapping, the admission and opt-in checks, and the monitor's start, progress and end handling.
- Not verified: behaviour on a real OctoPrint server and printer. If something does not work as expected, send printer diagnostics.

## Limits

- G-code (`.gcode`) files only.
- One tool and the heated bed are read. Multiple tools, SD card files and OctoPrint plugins are not used.
- O.D.I.N. never guesses the printer model; OctoPrint does not report one.
- The camera is found only when OctoPrint's settings give a full `http://` or `https://` stream URL on the printer's own address. A relative address such as `/webcam/?action=stream` is not guessed; set the camera URL by hand instead.
- OctoPrint has no "finished" flag. A print is recorded as completed only when OctoPrint reports 100 % completion; if it does not, the print is recorded as cancelled rather than guessed to have succeeded.
