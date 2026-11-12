# ODIN for Home Assistant

Read-only print farm status from an ODIN server. Home Assistant talks only to ODIN and never connects to the printers, so it does not compete with ODIN for the printers' MQTT or camera connection slots.

## Requirements
- ODIN with the Home Assistant status endpoint (`/api/v1/integrations/home-assistant/status`), the release after 2.0.50.
- An ODIN API token with `read:printers` and `read:jobs` (or `read`). Create it in ODIN under Settings > API Tokens.
- Optional: a separate camera-only token (`read:cameras`) for camera entities through ODIN's relay.
- Next job, Waiting on, Out of service and the day and queue totals need an ODIN that reports status schema 2 (2.2.0). Against an older ODIN (schema 1) those entities report unknown and everything else works as before.

## Install
- **Manual:** copy `custom_components/odin` into your Home Assistant `config/custom_components/` and restart.
- **HACS:** not yet published as a HACS repository.

Then go to Settings > Devices & services > Add integration > ODIN, and enter the ODIN URL and the token.

## Entities
Each printer gets one device with these entities:
- Status: printing, paused, error, offline, idle.
- Progress, plus finish time (timestamp) and time remaining.
- Current job, current layer and total layers.
- Active errors.
- Next job: the name of the next queued job for this printer, unknown when nothing is queued. Attributes: `job_id`, `held`, `est_minutes`, `copies_on_plate`, `filaments` (slot, color, material, grams), `plate_index`, `plate_count`, `ams_status` (match, mismatch or unknown), `ams_slots`, `scheduled_start`.
- Waiting on: what is holding the printer back, one of `nothing`, `paused`, `out_of_service`, `clear_plate`, `blocked`, `held`. Attributes: `reason`, `since`, `pause_cause` (set when paused).
- Online (connectivity), Needs attention (problem) and Out of service (the printer is on a maintenance hold in ODIN).
- An Error event that fires when ODIN records a new printer error.
- Camera, only when a camera token is set and the printer has a camera.

The ODIN fleet device has:
- Printers printing, paused, in error and offline.
- Queue length, with the next unassigned jobs (up to 5) as the `next_unassigned` attribute.
- Plates done today and Plates failed today (counted since local midnight on the ODIN server).
- Queued hours (estimated hours in the queue, 1 decimal) and Queued jobs without estimate.

Polling defaults to every 20 s (10-120 s in the options). v1 has no controls.

## Development
```bash
uv venv -p 3.13 .venv && VIRTUAL_ENV=.venv uv pip install pytest-homeassistant-custom-component PyTurboJPEG
.venv/bin/python -m pytest
```
