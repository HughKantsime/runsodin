"""
Vision frame save and cleanup utilities.

Extracted from PrinterVisionThread and VisionMonitorDaemon to isolate
all filesystem operations for vision frame management.
"""

import json
import logging
import os
import re
import uuid
from contextlib import ExitStack
import time
from datetime import datetime, timezone
from typing import Optional

import cv2
from sqlalchemy import text

# Resolved at use time: never a stale engine bound at import.
from core.db import live_engine as engine
from core.db_compat import sql

log = logging.getLogger('vision_monitor')

VISION_FRAMES_DIR = '/data/vision_frames'


# The root's ancestors and directory owners are trusted. Each namespace component
# below the root is opened without following symlinks and pinned for this call.
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_MAX_JPEG_BYTES = 32 * 1024 * 1024
_DETECTION_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def _identity(stat):
    return stat.st_dev, stat.st_ino


def _open_child_directory(parent_fd, name):
    try:
        os.mkdir(name, mode=0o750, dir_fd=parent_fd)
    except FileExistsError:
        pass
    return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)


def _verify_namespace(root_path, directories):
    """Verify the path still names the pinned directories, without following links."""
    with ExitStack() as stack:
        fd = os.open(root_path, _DIRECTORY_FLAGS)
        stack.callback(os.close, fd)
        if _identity(os.fstat(fd)) != _identity(os.fstat(directories[0][1])):
            raise OSError("Vision frame root changed during save")
        for name, pinned_fd in directories[1:]:
            fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=fd)
            stack.callback(os.close, fd)
            if _identity(os.fstat(fd)) != _identity(os.fstat(pinned_fd)):
                raise OSError("Vision frame directory changed during save")


def _save_frame(printer_id, frame, detection_type=None):
    if type(printer_id) is not int or printer_id <= 0:
        raise ValueError("printer_id must be a positive integer")
    if detection_type is not None and (
        not isinstance(detection_type, str) or not _DETECTION_NAME.fullmatch(detection_type)
    ):
        raise ValueError("Invalid detection type")
    quality = 85 if detection_type is not None else 80
    ok, encoded = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok or encoded is None:
        raise ValueError("JPEG encoding failed")
    if encoded.nbytes > _MAX_JPEG_BYTES:
        raise ValueError("JPEG encoding produced an oversized frame")
    payload = encoded.tobytes()
    if not payload or len(payload) > _MAX_JPEG_BYTES:
        raise ValueError("JPEG encoding produced an empty or oversized frame")

    root_path = os.path.abspath(VISION_FRAMES_DIR)
    # Only the configured root may need recursive creation; its ancestors are trusted.
    os.makedirs(root_path, mode=0o750, exist_ok=True)
    with ExitStack() as stack:
        root_fd = os.open(root_path, _DIRECTORY_FLAGS)
        stack.callback(os.close, root_fd)
        directories = [(None, root_fd)]
        printer_name = str(printer_id)
        target_fd = _open_child_directory(root_fd, printer_name)
        stack.callback(os.close, target_fd)
        directories.append((printer_name, target_fd))
        if detection_type is None:
            target_fd = _open_child_directory(target_fd, 'training')
            stack.callback(os.close, target_fd)
            directories.append(('training', target_fd))
        timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')
        suffix = f"_{detection_type}" if detection_type is not None else ''
        token = uuid.uuid4().hex
        filename = f"{timestamp}{suffix}_{token}.jpg"
        temporary = f".frame-{token}.tmp"
        temp_owned = False
        published = False
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=target_fd)
            temp_owned = True
            try:
                remaining = memoryview(payload)
                while remaining:
                    written = os.write(fd, remaining[:1024 * 1024])
                    if written <= 0:
                        raise OSError("JPEG write made no progress")
                    remaining = remaining[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            _verify_namespace(root_path, directories)
            os.link(temporary, filename, src_dir_fd=target_fd, dst_dir_fd=target_fd,
                    follow_symlinks=False)
            published = True
            os.unlink(temporary, dir_fd=target_fd)
            temp_owned = False
            os.fsync(target_fd)
            _verify_namespace(root_path, directories)
            if detection_type is None:
                return f"{printer_name}/training/{filename}"
            return f"{printer_name}/{filename}"
        except BaseException:
            # Remove only names created by this call, through its pinned directory.
            for name, owned in ((temporary, temp_owned), (filename, published)):
                if owned:
                    try:
                        os.unlink(name, dir_fd=target_fd)
                    except OSError:
                        log.exception("Could not remove failed vision frame publication")
            raise


def save_detection_frame(printer_id: int, frame, detection_type: str) -> str:
    """Publish a unique JPEG and return its relative path only after success."""
    if not isinstance(detection_type, str):
        raise ValueError("Invalid detection type")
    return _save_frame(printer_id, frame, detection_type)


def save_training_frame(printer_id: int, frame) -> str:
    """Publish a unique training JPEG and return its relative path; raise on failure."""
    return _save_frame(printer_id, frame)


def cleanup_old_frames(frames_dir: str = VISION_FRAMES_DIR) -> None:
    """
    Delete detection frames older than the configured retention period.
    Reads retention_days from system_config (default 30).
    Clears frame_path on old vision_detections records but keeps the metadata.
    Also cleans up training frames via cleanup_training_frames().
    """
    try:
        with engine.begin() as conn:
            # Retention comes from the global settings written by PATCH /api/vision/settings
            # ('vision_settings'.retention_days); the legacy 'vision_retention_days' key is the
            # fallback. Default 30.
            retention_days = 30
            rows = dict(conn.execute(
                text("SELECT key, value FROM system_config WHERE key IN ('vision_settings', 'vision_retention_days')")
            ).fetchall())

            def _decode(value):
                return json.loads(value) if isinstance(value, str) else value

            try:
                settings = _decode(rows.get('vision_settings'))
            except ValueError:
                settings = None
            configured = settings.get('retention_days') if isinstance(settings, dict) else None
            if configured is None and rows.get('vision_retention_days') is not None:
                try:
                    configured = _decode(rows['vision_retention_days'])
                except ValueError:
                    configured = None
            try:
                if configured is not None and int(configured) >= 1:
                    retention_days = int(configured)
            except (ValueError, TypeError):
                pass

            # Find old detections with frame paths
            _cutoff_sql = sql.now_offset(f"-{retention_days} days")
            old_rows = conn.execute(
                # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
                text(f"""SELECT id, frame_path FROM vision_detections
                WHERE created_at < {_cutoff_sql}
                  AND frame_path IS NOT NULL""")
            ).mappings().fetchall()

            deleted = 0
            for old in old_rows:
                fpath = os.path.join(frames_dir, old['frame_path'])
                if os.path.isfile(fpath):
                    os.remove(fpath)
                    deleted += 1

            # Clear frame_path on old records (keep detection metadata)
            if old_rows:
                ids = [str(r['id']) for r in old_rows]
                conn.execute(
                    text(f"UPDATE vision_detections SET frame_path = NULL "  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
                    f"WHERE id IN ({','.join(ids)})")
                )
            # A training sample is only its frame; drop samples whose frame expired.
            conn.execute(text("DELETE FROM vision_detections WHERE status = 'training' AND frame_path IS NULL"))

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
