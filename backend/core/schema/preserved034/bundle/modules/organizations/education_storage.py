"""Read-only Education upload filesystem diagnostics and shared reserve policy."""
from pathlib import Path
import shutil
import os
from decimal import Decimal, InvalidOperation, ROUND_CEILING

MIN_FREE_BYTES = 10 * 1024 * 1024 * 1024


class StorageConfigurationError(ValueError):
    """Invalid administrator reserve setting; never fall back permissively."""


def configured_floor_bytes() -> tuple[int, bool]:
    raw = os.environ.get("EDUCATION_MIN_FREE_GIB")
    if raw is None:
        return MIN_FREE_BYTES, False
    try:
        if not raw.strip() or len(raw) > 64:
            raise ValueError
        value = Decimal(raw.strip())
        if not value.is_finite() or value < 1 or value >= Decimal(2**63) / Decimal(1024**3):
            raise ValueError
        floor = int((value * Decimal(1024**3)).to_integral_value(rounding=ROUND_CEILING))
        if floor >= 2**63:
            raise ValueError
        return floor, True
    except (InvalidOperation, ValueError, OverflowError):
        raise StorageConfigurationError(
            "EDUCATION_MIN_FREE_GIB must be a finite number of GiB, at least 1 and below 8589934592."
        ) from None


def required_free_bytes(total: int, *, floor: int | None = None) -> int:
    if floor is None:
        floor, _ = configured_floor_bytes()
    return max(floor, int(total * 0.10))


def storage_readiness(storage_root: Path) -> dict:
    # A new installation may not have uploaded yet. Measure its existing parent
    # without creating the upload directory or exposing host paths to the UI.
    probe = storage_root
    try:
        floor, overridden = configured_floor_bytes()
    except StorageConfigurationError:
        return {"status": "configuration_error", "total_bytes": None, "free_bytes": None,
                "reserve_bytes": None, "upload_headroom_bytes": None,
                "upload_directory_exists": None, "configured_min_free_bytes": None,
                "administrator_override": True}
    try:
        root_exists = probe.exists()
        while not probe.exists():
            parent = probe.parent
            if parent == probe:
                raise OSError("No existing upload filesystem ancestor")
            probe = parent
        if not probe.is_dir():
            raise OSError("Upload filesystem probe is not a directory")
        usage = shutil.disk_usage(probe)
        reserve = required_free_bytes(usage.total, floor=floor)
        return {
            "configured_min_free_bytes": floor,
            "administrator_override": overridden,
            "status": "ready" if usage.free > reserve else "blocked",
            "total_bytes": usage.total,
            "free_bytes": usage.free,
            "reserve_bytes": reserve,
            "upload_headroom_bytes": max(0, usage.free - reserve),
            "upload_directory_exists": root_exists,
        }
    except OSError:
        return {"status": "unknown", "total_bytes": None, "free_bytes": None,
                "reserve_bytes": None, "upload_headroom_bytes": None,
                "upload_directory_exists": None, "configured_min_free_bytes": floor,
                "administrator_override": overridden}
