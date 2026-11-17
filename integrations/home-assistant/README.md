# ODIN for Home Assistant

Print farm status and plate clearing from an ODIN server. Home Assistant talks only to ODIN and never connects to the printers, so it does not compete with ODIN for the printers' MQTT or camera connection slots.

## Requirements
- ODIN with the Home Assistant status endpoint (`/api/v1/integrations/home-assistant/status`), the release after 2.0.50.
- An ODIN API token with `read:printers` and `read:jobs` (or `read`). Create it in ODIN under Settings > API Tokens.
- Optional: a separate camera-only token (`read:cameras`) for camera entities through ODIN's relay.
- Next job, Waiting on, Out of service and the day and queue totals need an ODIN that reports status schema 2 (2.2.0). Against an older ODIN (schema 1) those entities report unknown and everything else works as before.
- Spools loaded, Last print finished, Plate cleared, the Status time attributes and the Next job attributes `queued_after`, `short_spools` and `ready` (integration 0.3.0) need the ODIN release after 2.2.0. Against an older ODIN they report unknown and everything else works as before.
- Plates that did not complete today (integration 0.5.0) needs the combined count from ODIN. Against an older server that omits it, this sensor is unavailable.
- Plates stopped today (integration 0.4.0) needs ODIN 2.3.0 or later. Against an older ODIN that does not send the count, this sensor is unavailable.

## Install
- **Manual:** copy `custom_components/odin` into your Home Assistant `config/custom_components/` and restart.
- **HACS:** not yet published as a HACS repository.

Then go to Settings > Devices & services > Add integration > ODIN, and enter the ODIN URL and the token.

## Entities
Each printer gets one device with these entities:
- Status: printing, paused, error, offline, idle. A recorded error no longer shows as `error` once a print has started or completed after it. Attributes: `last_completed_at` (end of the last completed print) and `last_error_at` (when the last error was recorded).
- Progress, plus finish time (timestamp) and time remaining.
- Current job, current layer and total layers.
- Active errors.
- Next job: the name of the next queued job for this printer, unknown when nothing is queued. Attributes: `job_id`, `held`, `est_minutes`, `copies_on_plate`, `filaments` (slot, color, material, grams), `plate_index`, `plate_count`, `ams_status` (match, mismatch or unknown), `ams_slots`, `scheduled_start`, `queued_after` (how many more jobs are queued for this printer after this one), `short_spools` (the spools ODIN's filament check finds too low for this job: `slot`, `needed_g`, `remaining_g`; an empty list when every spool has enough, unknown when the check is off or a weight is not known) and `ready` (true when the loaded colours match, no spool is short, and the job is neither held nor blocked; false when one of those fails; unknown when the colours cannot be compared).
- Waiting on: what is holding the printer back, one of `nothing`, `paused`, `out_of_service`, `clear_plate`, `blocked`, `held`. Attributes: `reason`, `since`, `pause_cause` (set when paused).
- AMS humidity: the wettest AMS unit's humidity index, 1 (dry) to 5 (wet), the same order as the A-E scale on the printer. Attributes: `grade` (A-E), `wettest_unit`, `units` (each unit's index, grade, temperature and reading age). Unknown when no unit has a humidity sensor (AMS lite).
- Spools loaded: how many spool slots have filament in them. One sensor per printer; its `slots` attribute lists every slot (`slot`, `color_hex`, `name`, `material`, `remaining_percent`, `remaining_g`) with the same names the ODIN Printers page shows. Remaining amounts are unknown when ODIN does not know them, and on a printer another organization shares with you. The sensor is unknown on a printer that carries controlled work you are not allowed to see.
- Last print finished (timestamp): when the printer's last print ended, whatever its outcome. Unknown while a print is running.
- Plate cleared (timestamp): when the plate was confirmed clear after the last print. Unknown until it is confirmed, and again once the next print starts.
- Online (connectivity), Needs attention (problem) and Out of service (the printer is on a maintenance hold in ODIN).
- An Error event that fires when ODIN records a new printer error.
- Camera, only when a camera token is set and the printer has a camera.

The ODIN fleet device has:
- Printers printing, paused, in error and offline.
- Queue length, with the next unassigned jobs (up to 5) as the `next_unassigned` attribute.
- Plates done today and Plates that did not complete today (failed plus stopped, counted since midnight in ODIN's farm time zone). The separate Plates failed today and Plates stopped today sensors stay enabled by default.
- Queued hours (estimated hours in the queue, 1 decimal) and Queued jobs without estimate.

Polling defaults to every 20 s (10-120 s in the options).

### Plate cleared

Each printer has a **Plate cleared** button (`button.<printer>_plate_cleared`) and a **Plate needs clearing** binary sensor (`binary_sensor.<printer>_plate_needs_clearing`). If a job is waiting for the plate, pressing this releases it to start.

A waiting job exists only where the installation's queue puts jobs into the `waiting_plate_clear` state. The standard PostgreSQL job enum does not include that state; clearing a plate there records the finished print's confirmation without releasing a job.

The button calls ODIN's existing plate-clear route and refreshes the coordinator immediately after a successful press. It requires the `write:printers` token scope and `printers.edit` permission. The button is created for every printer: ODIN has no read that unambiguously reports the active token's permissions. A refused press names the required scope and permission and is never retried automatically.

Home Assistant has no confirmation step and sends no queue precondition: pressing releases whichever job is waiting at that moment. Older clients and scripts that omit the optional precondition keep this behavior. The web confirmation sends `expected_release_job_id` (the displayed ID, or null for no job); when the job is hidden it sends only `expected_has_waiting_job: true`, checking the already-disclosed presence without exposing an ID or name. A changed expectation returns 409 with the current reader-visible `plate_clear` state and requires confirmation again; it is not retried automatically.

The Status entity includes the feed's `plate_clear` object as an attribute: `needed`, `releases_job` (ID and display name, or null), `last_print_ended_at`, and `has_waiting_job`. A hidden job keeps its name and ID private; `has_waiting_job` still warns that clearing the plate can release work. On older ODIN versions without this object, the binary sensor reports unknown.

## Status feed day totals
`GET /api/v1/integrations/home-assistant/status` returns `fleet.today` with `plates_done`,
`plates_failed`, `plates_stopped`, `plates_did_not_complete` and `since` (farm midnight as a UTC timestamp). These count
print records that ended since that midnight: `plates_done` counts `completed`,
`plates_failed` counts `failed`, `plates_stopped` counts `cancelled`, and
`plates_did_not_complete` is failed plus stopped. Only visible printers and jobs count; a print without a scheduled job counts
with its printer. Each count is 0 when no records qualify. The combined field is additive;
the status schema version stays 2. Success rates in generic ODIN analytics use
completed / (completed + did not complete); Education usage and program reports keep their existing formula.

## Development
```bash
uv venv -p 3.13 .venv && VIRTUAL_ENV=.venv uv pip install pytest-homeassistant-custom-component PyTurboJPEG
.venv/bin/python -m pytest
```
