"""Capture writers — PNG/MP4/GIF emitters plus a manifest emitter that
records what was captured (scene id, scenario, commit hash, target URL,
viewport, timestamp) alongside each output bundle.

Wake 3 deliverable. Wake 1 is signatures only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class CaptureManifest:
    scene_id: str
    scenario: str
    target: str
    base_url: str
    viewport_w: int
    viewport_h: int
    git_sha: str
    captured_at_iso: str
    outputs: list[str]


def write_png(image_bytes: bytes, dest: Path) -> Path:
    raise NotImplementedError("write_png: Wake 3 deliverable")


def write_mp4(frames_dir: Path, dest: Path, *, fps: int = 30) -> Path:
    raise NotImplementedError("write_mp4: Wake 3 deliverable")


def write_gif(mp4_path: Path, dest: Path, *, fps_cap: int = 60) -> Path:
    raise NotImplementedError("write_gif: Wake 3 deliverable")


def write_manifest(manifest: CaptureManifest, dest: Path) -> Path:
    raise NotImplementedError("write_manifest: Wake 3 deliverable")
