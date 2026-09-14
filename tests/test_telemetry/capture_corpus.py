"""Resolve exhaustive private captures or sanitized committed corpus slices."""

from __future__ import annotations

import os
from pathlib import Path


COMMITTED_CORPUS = Path(__file__).parent.parent / "fixtures" / "telemetry"
COMMITTED_SLICES = {
    "bambu-a1.jsonl": "bambu-a1-happy-path.jsonl",
    "bambu-h2d.jsonl": "bambu-h2d-recovery.jsonl",
    "bambu-p1s.jsonl": "bambu-p1s-contract.jsonl",
    "bambu-x1c.jsonl": "bambu-x1c-ams-swap.jsonl",
}


def capture_path(printer_file: str) -> Path:
    """Use an explicitly configured full corpus, else a committed safe slice."""
    configured = os.getenv("ODIN_TELEMETRY_CAPTURE_DIR")
    if configured:
        path = Path(configured) / printer_file
        if not path.is_file():
            raise AssertionError(f"configured telemetry capture is missing: {path}")
        return path
    path = COMMITTED_CORPUS / COMMITTED_SLICES[printer_file]
    if not path.is_file():
        raise AssertionError(f"committed telemetry corpus slice is missing: {path}")
    return path
