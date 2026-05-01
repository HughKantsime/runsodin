# odin_capture — ODIN marketing-asset capture pipeline

Canonical factory for ODIN App Store screenshots, HN/Reddit cards, blog
hero images, Discord embeds, social videos, and marketing GIFs.

Real fixture replay against the demo MQTT publisher + live UI render in
headless Chromium → deterministic PNG/MP4/GIF outputs. Replaces fabricated
gpt-image-2 dashboards.

## Status

Wake 1 (this commit) lands scaffolding only. End-to-end capture lands in
Wake 2 with the first scene (`fleet-overview`). Tracking issue: **ODIN-142**.

## Quickstart (Wake 1)

```bash
pip install -r scripts/capture/requirements.txt
playwright install chromium
python -m scripts.capture.odin_capture.cli list-scenes
# → (no scenes registered yet — Wake 2 ships fleet-overview.yaml)
```

## Layout

```
scripts/capture/
  odin_capture/        Python package (CLI + helpers)
    cli.py             Entry point: python -m scripts.capture.odin_capture.cli
    config.py          Repo paths + target backend resolution
    processes.py       Boot/teardown of the docker-compose demo stack
    auth.py            Programmatic login → JWT session cookie
    replay.py          Wraps backend/modules/printers/telemetry/demo_cli
    browser.py         Playwright launch + navigate + wait helpers
    scenes.py          Scene YAML loader + Pydantic models
    outputs.py         PNG/MP4/GIF writers + manifest emitter
    ffmpeg.py          Subprocess wrappers for MP4 + GIF encoding
  scenes/              Scene YAMLs (Wake 2+)
  presets/             Output presets — App Store, social cards, website (Wake 2+)
  README.md            This file
  requirements.txt     Pinned deps
```

## Scene model

Each YAML under `scenes/` is validated by `odin_capture.scenes.Scene`
(Pydantic). Required fields:

- `id`, `title`, `target_route`, `scenario`
- `viewport: {width, height}`
- `wait_for: {selector, ...}`
- `captures: [{name, kind, ...}]`
- `formats: [png|mp4|gif, ...]`

## Targets

- `--target local` (default): the pipeline boots `ops/demo/docker-compose.demo.yml`
  itself. Deterministic, CI-able, no network dependency once the image is local.
- `--target demo`: points at `https://demo.subsystem.app`. Useful for
  one-off "what App Reviewers actually see" smokes; not deterministic.

## Phasing (per ODIN-142)

- ✅ **Wake 1**: scaffolding, CLI skeleton, scene/replay/processes stubs.
- ⏳ **Wake 2**: first scene end-to-end (`fleet-overview`) producing a real PNG.
- ⏳ **Wake 3**: video output + scenes 2 (`printer-detail-ams`) and 3 (`alert-failure`).
- ⏳ **Wake 4**: README polish, Makefile alias, demo-target smoke, ODIN-141 A2 handoff.

## Outputs

`captures/out/` is gitignored. Curated final assets live under
`captures/manifests/` (small JSON manifests), with the actual PNG/MP4/GIF
copied into `odin-site/` or wherever they're consumed.
