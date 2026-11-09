# ODIN for Home Assistant

Read-only print farm status from an ODIN server. Home Assistant talks only to ODIN and never connects to the printers, so it does not compete with ODIN for the printers' MQTT or camera connection slots.

## Requirements
- ODIN with the Home Assistant status endpoint (`/api/v1/integrations/home-assistant/status`), the release after 2.0.50.
- An ODIN API token with `read:printers` and `read:jobs` (or `read`). Create it in ODIN under Settings > API Tokens.
- Optional: a separate camera-only token (`read:cameras`) for camera entities through ODIN's relay.

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
- Online (connectivity) and Needs attention (problem).
- An Error event that fires when ODIN records a new printer error.
- Camera, only when a camera token is set and the printer has a camera.

The ODIN fleet device has: printers printing, paused, in error and offline, plus queue length.

Polling defaults to every 20 s (10-120 s in the options). v1 has no controls.

## Development
```bash
uv venv -p 3.13 .venv && VIRTUAL_ENV=.venv uv pip install pytest-homeassistant-custom-component PyTurboJPEG
.venv/bin/python -m pytest
```
