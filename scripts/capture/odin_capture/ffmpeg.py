"""FFmpeg helpers — MP4 encoding and MP4→GIF conversion.

Wake 3 deliverable. Subprocess-based by default (no python-ffmpeg
dependency required at the floor); ffmpeg-python is optional in
requirements.txt for callers who want a typed builder API.
"""

from __future__ import annotations

import shutil


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None
