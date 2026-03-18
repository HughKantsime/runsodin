#!/usr/bin/env python3
"""Record a scripted walkthrough of ODIN using Playwright video capture.

Navigates through key UI routes with dark-mode enabled, records the session,
then post-processes the raw WebM with FFmpeg (1.5x speed, H.264 MP4).

Environment variables
---------------------
ODIN_BASE_URL       Base URL of the ODIN instance (default: http://localhost:8000)
ODIN_ADMIN_USER     Admin username for login (default: admin)
ODIN_ADMIN_PASSWORD Admin password (required)
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL: str = os.environ.get("ODIN_BASE_URL", "http://localhost:8000").rstrip("/")
ADMIN_USER: str = os.environ.get("ODIN_ADMIN_USER", "admin")
ADMIN_PASSWORD: str | None = os.environ.get("ODIN_ADMIN_PASSWORD")

WALKTHROUGH: list[tuple[str, int]] = [
    ("/", 3000),
    ("/printers", 2500),
    ("/jobs", 2000),
    ("/timeline", 2000),
    ("/models", 2000),
    ("/spools", 2000),
    ("/orders", 2000),
    ("/analytics", 2500),
    ("/archives", 1500),
    ("/settings", 1500),
    ("/", 2000),  # closing shot
]

VIDEO_WIDTH = 1920
VIDEO_HEIGHT = 1080

MARKETING_DIR = Path(__file__).resolve().parent
RAW_VIDEO_DIR = MARKETING_DIR / "videos" / "_raw"
OUTPUT_VIDEO = MARKETING_DIR / "videos" / "walkthrough.mp4"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_jwt_token() -> str:
    """Authenticate against the ODIN API and return a JWT token."""
    login_url = f"{BASE_URL}/api/auth/login"
    resp = requests.post(
        login_url,
        json={"username": ADMIN_USER, "password": ADMIN_PASSWORD},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    token: str = data.get("access_token") or data.get("token") or ""
    if not token:
        raise RuntimeError(f"No token in login response: {data}")
    return token


def _inject_auth(page, token: str) -> None:
    """Inject JWT and user info into localStorage so the SPA recognises us."""
    # Decode the JWT payload to extract user metadata.
    payload_b64 = token.split(".")[1]
    payload_b64 += "=" * (4 - len(payload_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    user_json = json.dumps(
        {"username": payload.get("sub", "admin"), "role": payload.get("role", "admin")}
    )

    page.evaluate(
        f"""() => {{
        localStorage.setItem('token', '{token}');
        localStorage.setItem('user', {json.dumps(user_json)});
    }}"""
    )


def _ffmpeg_postprocess(raw_path: Path, output_path: Path) -> None:
    """Speed-up to 1.5x and convert to H.264 MP4 via FFmpeg.

    Tries with audio first; falls back to no-audio if the raw video has no
    audio stream (common with Playwright recordings).
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    base_cmd: list[str] = [
        "ffmpeg",
        "-y",
        "-i",
        str(raw_path),
    ]

    # Attempt 1 — with audio speed-up
    cmd_with_audio = base_cmd + [
        "-filter:v", "setpts=PTS/1.5",
        "-filter:a", "atempo=1.5",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(output_path),
    ]

    # Attempt 2 — no audio
    cmd_no_audio = base_cmd + [
        "-filter:v", "setpts=PTS/1.5",
        "-an",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "23",
        "-movflags", "+faststart",
        str(output_path),
    ]

    print("[ffmpeg] Attempting post-processing with audio ...")
    result = subprocess.run(
        cmd_with_audio,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print("[ffmpeg] Audio track unavailable, retrying without audio ...")
        result = subprocess.run(
            cmd_no_audio,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print("[ffmpeg] stderr:", result.stderr, file=sys.stderr)
            raise RuntimeError("FFmpeg post-processing failed")

    print(f"[ffmpeg] Output written to {output_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def record_walkthrough() -> Path:
    """Run the full recording pipeline and return the output path."""
    if not ADMIN_PASSWORD:
        raise SystemExit("ODIN_ADMIN_PASSWORD environment variable is required")

    # Ensure directories exist.
    RAW_VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_VIDEO.parent.mkdir(parents=True, exist_ok=True)

    print(f"[auth] Logging in as '{ADMIN_USER}' at {BASE_URL} ...")
    token = _get_jwt_token()
    print("[auth] Login successful")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": VIDEO_WIDTH, "height": VIDEO_HEIGHT},
            color_scheme="dark",
            record_video_dir=str(RAW_VIDEO_DIR),
            record_video_size={"width": VIDEO_WIDTH, "height": VIDEO_HEIGHT},
        )

        page = context.new_page()

        # Navigate to the base URL first so localStorage is on the right origin.
        page.goto(BASE_URL, wait_until="domcontentloaded")
        _inject_auth(page, token)

        # Reload so the app picks up the injected credentials.
        page.reload(wait_until="networkidle")

        for route, dwell_ms in WALKTHROUGH:
            url = f"{BASE_URL}{route}"
            print(f"[walk] {route} ({dwell_ms} ms)")
            page.goto(url, wait_until="networkidle")
            page.wait_for_timeout(dwell_ms)

        # Close context to finalise the video file.
        raw_video_path = Path(page.video.path())
        context.close()
        browser.close()

    print(f"[raw] Video saved to {raw_video_path}")

    # Post-process with FFmpeg.
    _ffmpeg_postprocess(raw_video_path, OUTPUT_VIDEO)

    # Clean up raw directory.
    shutil.rmtree(RAW_VIDEO_DIR, ignore_errors=True)
    print("[cleanup] Raw video directory removed")

    return OUTPUT_VIDEO


if __name__ == "__main__":
    out = record_walkthrough()
    print(f"\nDone! Walkthrough video: {out}")
