"""
Vision frame save and cleanup utilities.

Extracted from PrinterVisionThread and VisionMonitorDaemon to isolate
all filesystem operations for vision frame management.
"""

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional

import cv2

from core.db_utils import get_db

log = logging.getLogger('vision_monitor')

VISION_FRAMES_DIR = '/data/vision_frames'


def save_detection_frame(printer_id: int, frame, detection_type: str) -> str:
    """
    Save a detection frame JPEG to disk and return the relative path.

    Args:
        printer_id: DB printer ID (used as subdirectory name).
        frame: OpenCV numpy frame.
        detection_type: e.g. 'spaghetti', 'first_layer'.

    Returns:
        Relative path string suitable for storing in vision_detections.frame_path.
    """
    ts = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    rel_dir = f"{printer_id}"
    abs_dir = os.path.join(VISION_FRAMES_DIR, rel_dir)
    os.makedirs(abs_dir, exist_ok=True)
    filename = f"{ts}_{detection_type}.jpg"
    filepath = os.path.join(abs_dir, filename)
    cv2.imwrite(filepath, frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return f"{rel_dir}/{filename}"


def save_training_frame(printer_id: int, frame) -> None:
    """
    Save a frame for training data collection.

    Args:
        printer_id: DB printer ID.
        frame: OpenCV numpy frame.
    """
    ts = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')
    abs_dir = os.path.join(VISION_FRAMES_DIR, str(printer_id), 'training')
    os.makedirs(abs_dir, exist_ok=True)
    filepath = os.path.join(abs_dir, f"{ts}.jpg")
    cv2.imwrite(filepath, frame, [cv2.IMWRITE_JPEG_QUALITY, 80])


def cleanup_old_frames(frames_dir: str = VISION_FRAMES_DIR) -> None:
    """
    Delete detection frames older than the configured retention period.
    Reads retention_days from system_config (default 30).
    Clears frame_path on old vision_detections records but keeps the metadata.
    Also cleans up training frames via cleanup_training_frames().
    """
    try:
        with get_db(row_factory=sqlite3.Row) as conn:
            cur = conn.cursor()

            # Get retention days from global settings (default 30)
            cur.execute(
                "SELECT value FROM system_config WHERE key = 'vision_retention_days'"
            )
            row = cur.fetchone()
            retention_days = 30
            if row:
                try:
                    retention_days = int(json.loads(row['value']))
                except (ValueError, TypeError):
                    pass

            # Find old detections with frame paths
            cur.execute(
                """SELECT id, frame_path FROM vision_detections
                WHERE created_at < datetime('now', ? || ' days')
                  AND frame_path IS NOT NULL""",
                (f"-{retention_days}",)
            )
            old_rows = cur.fetchall()

            deleted = 0
            for old in old_rows:
                fpath = os.path.join(frames_dir, old['frame_path'])
                if os.path.isfile(fpath):
                    os.remove(fpath)
                    deleted += 1

            # Clear frame_path on old records (keep detection metadata)
            if old_rows:
                ids = [str(r['id']) for r in old_rows]
                cur.execute(
                    f"UPDATE vision_detections SET frame_path = NULL "
                    f"WHERE id IN ({','.join(ids)})"
                )
                conn.commit()

        if deleted:
            log.info(f"Cleaned up {deleted} old vision frames (>{retention_days} days)")

        # Also clean up training frames older than retention
        cleanup_training_frames(frames_dir, retention_days)

    except Exception as e:
        log.error(f"Frame cleanup error: {e}")


def cleanup_training_frames(frames_dir: str, retention_days: int) -> None:
    """Remove old training data frames that exceed the retention period."""
    cutoff = time.time() - (retention_days * 86400)
    for root, dirs, files in os.walk(frames_dir):
        if 'training' not in root:
            continue
        for f in files:
            fpath = os.path.join(root, f)
            try:
                if os.path.getmtime(fpath) < cutoff:
                    os.remove(fpath)
            except OSError:
                pass
