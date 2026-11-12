"""
Vigil training-frame capture policy (Stage 1 of the own-model plan).

Pure helpers shared by the detection thread, the monitor daemon and the settings API:
  - sampling interval (global default + per-printer override),
  - bed crop (fractions of the frame, applied to training frames only),
  - storage guard (stop saving when the frames volume is nearly full, resume on its own),
  - the "no-training" model category used to exclude work from the dataset,
  - per-printer config kept in system_config (no schema change).

The thread owns the queries on production tables; nothing here reads them.
"""

import json
import logging
import math
import os
import shutil
import threading
import time
from typing import Callable, Dict, Optional

from sqlalchemy import text

log = logging.getLogger('vision_monitor')

DEFAULT_TRAINING_INTERVAL = 120   # seconds between training frames (detection runs every ~10 s)
MAX_TRAINING_INTERVAL = 86400
DEFAULT_MIN_FREE_GB = 2.0
MAX_MIN_FREE_GB = 10240.0
BURST_FRAMES = 3                  # per detection: the tick before, the detection tick, the tick after
MIN_CROP_FRACTION = 0.05
EXCLUDE_CATEGORY = 'no-training'  # models.category that keeps a model's prints out of the dataset
CONFIG_PREFIX = 'vision_training_printer_'   # system_config key: CONFIG_PREFIX + printer id
GLOBAL_INTERVAL_KEY = 'training_capture_interval_sec'
GLOBAL_MIN_FREE_KEY = 'training_min_free_gb'
_CROP_KEYS = ('x', 'y', 'w', 'h')
_TOLERANCE = 1e-9


# ---------------------------------------------------------------- numbers

def training_interval(value, default: int = DEFAULT_TRAINING_INTERVAL) -> int:
    """A valid training interval in seconds (1..86400), else `default`."""
    if type(value) is int and 1 <= value <= MAX_TRAINING_INTERVAL:
        return value
    return default


def _number_in_range(value, low: float, high: float) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and low <= value <= high)


def min_free_bytes(value) -> int:
    """Free space (bytes) the frames volume must keep. `value` is GB; 0 turns the guard off."""
    if _number_in_range(value, 0, MAX_MIN_FREE_GB):
        return int(value * 1024 ** 3)
    return int(DEFAULT_MIN_FREE_GB * 1024 ** 3)


def global_setting_error(updates: dict) -> Optional[str]:
    """Message for an invalid global training setting in a PATCH body, else None."""
    if GLOBAL_INTERVAL_KEY in updates and training_interval(updates[GLOBAL_INTERVAL_KEY], 0) == 0:
        return f"{GLOBAL_INTERVAL_KEY} must be a whole number from 1 to {MAX_TRAINING_INTERVAL}"
    if GLOBAL_MIN_FREE_KEY in updates and not _number_in_range(updates[GLOBAL_MIN_FREE_KEY], 0, MAX_MIN_FREE_GB):
        return f"{GLOBAL_MIN_FREE_KEY} must be a number from 0 to {int(MAX_MIN_FREE_GB)} (0 turns the guard off)"
    return None


# ---------------------------------------------------------------- crop

def _fraction(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"training_crop {name} must be a number from 0 to 1")
    return round(float(value), 4)


def validate_crop(value) -> Dict[str, float]:
    """A crop rectangle as fractions of the frame: x, y (top-left) and w, h, all inside 0..1."""
    if not isinstance(value, dict) or set(value) != set(_CROP_KEYS):
        raise ValueError("training_crop must be an object with x, y, w and h (or null for the full frame)")
    x, y, w, h = (_fraction(value[key], key) for key in _CROP_KEYS)
    if not (0 <= x < 1 and 0 <= y < 1):
        raise ValueError("training_crop x and y must be at least 0 and below 1")
    if w < MIN_CROP_FRACTION or h < MIN_CROP_FRACTION or w > 1 or h > 1:
        raise ValueError(f"training_crop w and h must be from {MIN_CROP_FRACTION} to 1")
    if x + w > 1 + _TOLERANCE or y + h > 1 + _TOLERANCE:
        raise ValueError("training_crop must stay inside the frame (x + w and y + h at most 1)")
    return {'x': x, 'y': y, 'w': w, 'h': h}


def crop_box(shape, crop: Dict[str, float]):
    """Pixel box (x1, y1, x2, y2) for a validated crop on a frame of `shape`; never empty, never outside."""
    height, width = shape[:2]
    x1 = min(max(int(math.floor(crop['x'] * width)), 0), width - 1)
    y1 = min(max(int(math.floor(crop['y'] * height)), 0), height - 1)
    x2 = min(width, max(x1 + 1, int(round((crop['x'] + crop['w']) * width))))
    y2 = min(height, max(y1 + 1, int(round((crop['y'] + crop['h']) * height))))
    return x1, y1, x2, y2


def crop_frame(frame, crop: Optional[Dict[str, float]]):
    """The training copy of a frame: cropped to the bed when a crop is set, else the full frame."""
    if not crop:
        return frame
    x1, y1, x2, y2 = crop_box(frame.shape, crop)
    return frame[y1:y2, x1:x2].copy()


# ---------------------------------------------------------------- sampling

class TrainingSampler:
    """Decides when a scheduled training frame is due; the first one of a print is due at once."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._last: Optional[float] = None

    def due(self, interval: int) -> bool:
        return self._last is None or self._clock() - self._last >= interval

    def mark(self) -> None:
        self._last = self._clock()

    def reset(self) -> None:
        self._last = None


# ---------------------------------------------------------------- storage guard

def _existing_ancestor(path: str) -> str:
    path = os.path.abspath(path)
    while not os.path.exists(path) and os.path.dirname(path) != path:
        path = os.path.dirname(path)
    return path


class StorageGuard:
    """Refuses training frames while the frames volume has less than the minimum free.

    One guard serves every printer thread. `on_low` runs once when the volume drops below the
    minimum and again only after free space has recovered and dropped a second time. Saving
    resumes by itself as soon as free space is back at or above the minimum.
    """

    def __init__(self, free_bytes: Optional[Callable[[str], int]] = None):
        self._free_bytes = free_bytes or (lambda path: shutil.disk_usage(path).free)
        self._lock = threading.Lock()
        self._low = False

    @property
    def low(self) -> bool:
        return self._low

    def has_room(self, path: str, minimum: int, on_low: Optional[Callable[[int, int], None]] = None) -> bool:
        if minimum <= 0:
            with self._lock:
                self._low = False
            return True
        try:
            free = int(self._free_bytes(_existing_ancestor(path)))
        except Exception:
            # Free space unknown: fail closed, as the controlled-work check does.
            log.warning("Free space of the Vigil frames volume could not be read; training frame not saved")
            return False
        with self._lock:
            if free >= minimum:
                self._low = False
                return True
            first = not self._low
            self._low = True
        if first and on_low is not None:
            try:
                on_low(free, minimum)
            except Exception:
                log.exception("Vigil storage-guard alert could not be raised")
        return False


STORAGE_GUARD = StorageGuard()


# ---------------------------------------------------------------- exclusion

def excluded_category(category) -> bool:
    """True for a model category of 'no-training' (case-insensitive; comma/semicolon lists allowed)."""
    if not isinstance(category, str):
        return False
    return any(token.strip().lower() == EXCLUDE_CATEGORY for token in category.replace(';', ',').split(','))


# ---------------------------------------------------------------- stored configuration

def _as_dict(value) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def load_printer_configs(conn) -> Dict[int, dict]:
    """Per-printer training config ({printer_id: {'crop', 'interval_sec'}}) from system_config."""
    rows = conn.execute(text("SELECT key, value FROM system_config WHERE key LIKE :prefix"),
                        {'prefix': CONFIG_PREFIX + '%'}).fetchall()
    configs: Dict[int, dict] = {}
    for key, value in rows:
        suffix = str(key)[len(CONFIG_PREFIX):]
        if suffix.isdigit():
            configs[int(suffix)] = _as_dict(value)
    return configs


def resolve_config(global_settings, printer_config, failed: bool = False) -> dict:
    """The effective training capture settings for one printer.

    `crop_invalid` is True when a crop is stored but unusable, or when the per-printer config
    could not be read at all: the thread then saves no training frame rather than a full one.
    """
    glob = global_settings if isinstance(global_settings, dict) else {}
    own = printer_config if isinstance(printer_config, dict) else {}
    default_interval = training_interval(glob.get(GLOBAL_INTERVAL_KEY))
    crop = None
    invalid = bool(failed)
    if own.get('crop') is not None:
        try:
            crop = validate_crop(own['crop'])
        except ValueError:
            invalid = True
    return {
        'interval_sec': training_interval(own.get('interval_sec'), default_interval),
        'crop': crop,
        'crop_invalid': invalid,
        'min_free_bytes': min_free_bytes(glob.get(GLOBAL_MIN_FREE_KEY)),
    }


def validate_printer_training(body: dict) -> dict:
    """The training fields of a per-printer PATCH body, validated. Raises ValueError."""
    out: dict = {}
    if 'training_crop' in body:
        value = body['training_crop']
        out['training_crop'] = None if value is None else validate_crop(value)
    if 'training_interval_sec' in body:
        value = body['training_interval_sec']
        if value is not None and training_interval(value, 0) == 0:
            raise ValueError(f"training_interval_sec must be a whole number from 1 to {MAX_TRAINING_INTERVAL} (or null for the default)")
        out['training_interval_sec'] = value
    return out


def save_printer_training(db, printer_id: int, training: dict) -> None:
    """Merge validated training fields into the printer's system_config row (caller commits)."""
    from core.models import SystemConfig
    key = f"{CONFIG_PREFIX}{printer_id}"
    row = db.query(SystemConfig).filter(SystemConfig.key == key).first()
    merged = dict(_as_dict(row.value)) if row else {}
    if 'training_crop' in training:
        merged['crop'] = training['training_crop']
    if 'training_interval_sec' in training:
        merged['interval_sec'] = training['training_interval_sec']
    if row:
        row.value = merged   # a new object: in-place JSON edits are invisible to SQLAlchemy
    else:
        db.add(SystemConfig(key=key, value=merged))


def printer_training_view(db, printer_id: int) -> dict:
    """Training fields as the per-printer settings API returns them."""
    from core.models import SystemConfig
    row = db.query(SystemConfig).filter(SystemConfig.key == f"{CONFIG_PREFIX}{printer_id}").first()
    stored = _as_dict(row.value) if row else {}
    try:
        crop = validate_crop(stored['crop']) if stored.get('crop') is not None else None
    except ValueError:
        crop = None
    interval = stored.get('interval_sec')
    return {'training_crop': crop,
            'training_interval_sec': interval if training_interval(interval, 0) else None}
