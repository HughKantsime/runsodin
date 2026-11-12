"""Per-plate derived data of a sliced 3MF, cached on disk by the file's SHA-256.

This is derived data, re-creatable from the stored file, so it is a file cache, not a table:

    <cache_root>/<hash[:2]>/<hash>/facts.json
    <cache_root>/<hash[:2]>/<hash>/plate_<N>.png | plate_<N>.jpg
    <cache_root>/<hash[:2]>/<hash>/unavailable.json   (deterministic parse refusal; stops re-parsing)

``cache_root`` is PLATE_CACHE_ROOT, else ``plate_cache`` beside the print-file storage root
(PRINT_FILE_STORAGE_ROOT, default /data/print_files, so /data/plate_cache). Paths are built only from
a validated 64-hex hash and an int plate number; writes are temp file + os.replace; directories are
0700 and files 0600. Nothing here authorizes anyone: callers reach it only after
core.model_preview_access.accessible_preview_file (or an equivalent proof) for a row that still has
its blob, so a cached image is never reachable by hash alone.
"""
import base64
import json
import logging
import math
import os
import re
import stat
import tempfile
from collections import namedtuple
from pathlib import Path

from core.thumbnail_check import MAX_THUMBNAIL_BYTES, thumbnail_kind
from modules.models_library.threemf_parser import MAX_FACT_PLATES, MAX_TOTAL_THUMBNAIL_BYTES

log = logging.getLogger("odin.api")

FACTS_VERSION = 1
MAX_PLATE_NUMBER = 9999
MAX_FACTS_BYTES = 2**20
_HASH = re.compile(r"[0-9a-f]{64}")
_COLOR = re.compile(r"#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?")
_EXTENSIONS = {"png": "png", "jpeg": "jpg"}
_MEDIA_TYPES = {"png": "image/png", "jpeg": "image/jpeg"}
_REFUSALS = frozenset({"bad_zip", "too_large", "too_many_members"})   # deterministic: never worth retrying
_PLATE_KEYS = {"index", "filaments", "print_time_seconds", "weight_grams", "layer_height", "warnings", "thumbnail_b64"}

CachedImage = namedtuple("CachedImage", "content media_type")


# --------------------------------------------------------------------------- paths

def cache_root() -> Path:
    configured = os.environ.get("PLATE_CACHE_ROOT")
    if configured:
        root = Path(configured)
    else:
        root = Path(os.environ.get("PRINT_FILE_STORAGE_ROOT") or "/data/print_files").parent / "plate_cache"
    if not root.is_absolute():
        raise ValueError("plate cache root must be an absolute path")
    return root


def _valid_hash(file_hash) -> bool:
    return type(file_hash) is str and _HASH.fullmatch(file_hash) is not None


def entry_dir(file_hash) -> Path:
    if not _valid_hash(file_hash):
        raise ValueError("invalid file hash")
    return cache_root() / file_hash[:2] / file_hash


def _plate_number(number) -> int:
    if type(number) is not int or not 1 <= number <= MAX_PLATE_NUMBER:
        raise ValueError("invalid plate number")
    return number


def _prepare_directory(directory: Path) -> None:
    """root/prefix/entry as real directories, mode 0700 (a symlink or file in the way is refused)."""
    root = cache_root()
    os.makedirs(root.parent, exist_ok=True)
    for path in (root, directory.parent, directory):
        try:
            os.mkdir(path, 0o700)
            os.chmod(path, 0o700)
        except FileExistsError:
            pass
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            raise OSError("plate cache path is not a directory")


def _atomic_write(directory: Path, name: str, content: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, directory / name)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _read_regular(path: Path, limit: int):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            data = handle.read(limit + 1)
        return data if len(data) <= limit else None
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _sniff(content: bytes):
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if content.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    return None


# --------------------------------------------------------------------------- wire validation

def _text_ok(value, limit=128):
    return value is None or (type(value) is str and len(value) <= limit)


def _number_ok(value, high):
    return value is None or (type(value) in (int, float) and math.isfinite(value) and 0 <= value <= high)


def _filament_ok(item):
    return (type(item) is dict and set(item) == {"slot", "type", "color", "grams"}
            and type(item["slot"]) is int and 0 <= item["slot"] <= 1024
            and type(item["type"]) is str and len(item["type"]) <= 128
            and (item["color"] is None or (type(item["color"]) is str and bool(_COLOR.fullmatch(item["color"]))))
            and _number_ok(item["grams"], 1e7))


def _warning_ok(item):
    return type(item) is dict and set(item) == {"message", "level", "code"} and all(_text_ok(v) for v in item.values())


def validate_wire(parsed):
    """Schema-check the child's frame. Returns (status, facts).

    ('ok', facts) with every thumbnail decoded and re-validated as (kind, bytes); a deterministic
    refusal status with None; anything else ('failed', None). The child is resource-bounded, not
    trusted, so nothing it sends reaches disk unchecked."""
    if type(parsed) is not dict:
        return "failed", None
    status = parsed.get("status")
    if status != "ok":
        return (status, None) if status in _REFUSALS and set(parsed) == {"status"} else ("failed", None)
    if set(parsed) != {"status", "slicer", "plates"}:
        return "failed", None
    slicer, plates = parsed["slicer"], parsed["plates"]
    if slicer is not None and not (type(slicer) is dict and set(slicer) == {"name", "version"}
                                   and all(_text_ok(v) for v in slicer.values())):
        return "failed", None
    if type(plates) is not list or len(plates) > MAX_FACT_PLATES:
        return "failed", None
    checked, total, previous = [], 0, 0
    for plate in plates:
        if type(plate) is not dict or set(plate) != _PLATE_KEYS:
            return "failed", None
        index = plate["index"]
        if type(index) is not int or not previous < index <= MAX_PLATE_NUMBER:
            return "failed", None
        previous = index
        seconds = plate["print_time_seconds"]
        if not (seconds is None or (type(seconds) is int and 0 <= seconds <= 10**8)):
            return "failed", None
        filaments, warnings = plate["filaments"], plate["warnings"]
        if (type(filaments) is not list or len(filaments) > 64 or not all(_filament_ok(f) for f in filaments)
                or not _number_ok(plate["weight_grams"], 1e7) or not _number_ok(plate["layer_height"], 100)
                or not (warnings is None or (type(warnings) is list and len(warnings) <= 32
                                             and all(_warning_ok(w) for w in warnings)))):
            return "failed", None
        encoded, thumbnail = plate["thumbnail_b64"], None
        if encoded is not None:
            if type(encoded) is not str or len(encoded) > 4 * ((MAX_THUMBNAIL_BYTES + 2) // 3):
                return "failed", None
            try:
                content = base64.b64decode(encoded, validate=True)
            except ValueError:
                return "failed", None
            kind = thumbnail_kind(content)
            total += len(content)
            if kind is None or total > MAX_TOTAL_THUMBNAIL_BYTES:
                return "failed", None
            thumbnail = (kind, content)
        checked.append({**{k: v for k, v in plate.items() if k != "thumbnail_b64"}, "thumbnail": thumbnail})
    return "ok", {"slicer": slicer, "plates": checked}


# --------------------------------------------------------------------------- store / read

def _valid_facts(document, file_hash) -> bool:
    if type(document) is not dict or type(document.get("version")) is not int or document["version"] != FACTS_VERSION:
        return False
    if document.get("file_hash") != file_hash:
        return False
    plates = document.get("plates")
    return (type(plates) is list and len(plates) <= MAX_FACT_PLATES and document.get("plate_count") == len(plates)
            and all(type(p) is dict and type(p.get("index")) is int and type(p.get("has_thumbnail")) is bool
                    for p in plates))


def store(file_hash, facts: dict) -> dict:
    """Write validated facts (the second item of validate_wire). Thumbnails first, facts.json last,
    so a readable facts.json always has its images. Raises ValueError (bad hash/plate) or OSError."""
    directory = entry_dir(file_hash)
    for plate in facts["plates"]:
        _plate_number(plate["index"])
    _prepare_directory(directory)
    plates = []
    for plate in facts["plates"]:
        number, thumbnail = plate["index"], plate["thumbnail"]
        if thumbnail is not None:
            kind, content = thumbnail
            _atomic_write(directory, f"plate_{number}.{_EXTENSIONS[kind]}", content)
        plates.append({"index": number, "has_thumbnail": thumbnail is not None, "filaments": plate["filaments"],
                       "print_time_seconds": plate["print_time_seconds"], "weight_grams": plate["weight_grams"],
                       "layer_height": plate["layer_height"], "warnings": plate["warnings"]})
    document = {"version": FACTS_VERSION, "file_hash": file_hash, "slicer": facts["slicer"],
                "plate_count": len(plates), "plates": plates}
    _atomic_write(directory, "facts.json", json.dumps(document, sort_keys=True, allow_nan=False).encode())
    try:
        os.unlink(directory / "unavailable.json")
    except OSError:
        pass
    return document


def mark_unavailable(file_hash, reason: str) -> None:
    directory = entry_dir(file_hash)
    _prepare_directory(directory)
    _atomic_write(directory, "unavailable.json", json.dumps({"version": FACTS_VERSION, "reason": reason}).encode())


def is_unavailable(file_hash) -> bool:
    try:
        return _read_regular(entry_dir(file_hash) / "unavailable.json", 4096) is not None
    except ValueError:
        return False


def read_facts(file_hash):
    """The cached facts document, or None (absent, corrupt, wrong hash or version: treated as absent)."""
    try:
        directory = entry_dir(file_hash)
    except ValueError:
        return None
    data = _read_regular(directory / "facts.json", MAX_FACTS_BYTES)
    if data is None:
        return None
    try:
        document = json.loads(data)
    except (ValueError, RecursionError):
        return None
    return document if _valid_facts(document, file_hash) else None


def read_thumbnail(file_hash, plate):
    """CachedImage for one plate, or None. Only plates the facts say have a thumbnail."""
    try:
        directory = entry_dir(file_hash)
        number = _plate_number(plate)
    except ValueError:
        return None
    facts = read_facts(file_hash)
    if facts is None or not any(p["index"] == number and p["has_thumbnail"] for p in facts["plates"]):
        return None
    for kind, extension in _EXTENSIONS.items():
        content = _read_regular(directory / f"plate_{number}.{extension}", MAX_THUMBNAIL_BYTES)
        if content is not None and _sniff(content) == kind:
            return CachedImage(content, _MEDIA_TYPES[kind])
    return None


def store_analysis(file_hash, analysis):
    """Persist a platefacts analysis. Never raises: a cache failure is logged and means 'no cache'."""
    try:
        status = getattr(analysis, "status", None)
        if status == "ok" and getattr(analysis, "facts", None) is not None:
            return store(file_hash, analysis.facts)
        if status in _REFUSALS:
            mark_unavailable(file_hash, status)
    except (OSError, ValueError) as exc:
        log.warning("Plate cache write failed (%s)", type(exc).__name__)
    return None


# --------------------------------------------------------------------------- rows

def eligible(row) -> bool:
    """Only a row with a file hash and a still-present stored 3MF blob has a cache."""
    stored = getattr(row, "stored_path", None)
    return (_valid_hash(getattr(row, "file_hash", None)) and type(stored) is str
            and stored.lower().endswith(".3mf") and getattr(row, "blob_state", None) == "present")


def facts_for_row(row, *, timeout: float = 30):
    """Cached facts, or built now from the stored blob (backfill). None means 'not available'.

    The blob is staged and re-hashed first, so a cache entry always derives from bytes that hash to
    its key. Raises mesh_isolation.Busy when the 3MF pool is full (caller answers 429)."""
    if not eligible(row):
        return None
    cached = read_facts(row.file_hash)
    if cached is not None:
        return cached
    if is_unavailable(row.file_hash):
        return None
    from core.print_payload_snapshot import PayloadUnavailable, stage_payload
    from modules.models_library.mesh_isolation import plate_facts_blocking
    try:
        with stage_payload(row.stored_path, row.file_hash) as snapshot:
            analysis = plate_facts_blocking(snapshot.path, timeout=timeout)
    except PayloadUnavailable:
        return None
    return store_analysis(row.file_hash, analysis)


def thumbnail_for_row(row, plate):
    """Cache-only: a missing cache is 'not available', never a parse."""
    if not eligible(row):
        return None
    return read_thumbnail(row.file_hash, plate)


# --------------------------------------------------------------------------- cleanup

def sweep(db, limit: int = 5000) -> int:
    """Delete cache entries no present print file refers to any more. Returns how many went.

    The cache is derived data keyed by file hash and shared by every row with that hash, so it is
    removed here, in one place, whichever way the last row went (delete, Education term cleanup,
    organization deletion, controlled-part retention). Nothing is served from an orphaned entry
    in the meantime: every read goes through a present row first.
    """
    import shutil
    from sqlalchemy import text
    try:
        root = cache_root()
        shards = sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink())
    except (OSError, ValueError):
        return 0
    removed = seen = 0
    for shard in shards:
        try:
            entries = sorted(p for p in shard.iterdir() if p.is_dir() and not p.is_symlink())
        except OSError:
            continue
        for entry in entries:
            if seen >= limit:
                return removed
            seen += 1
            if not _valid_hash(entry.name) or entry.name[:2] != shard.name:
                continue  # not ours: never delete what this module did not name
            alive = db.execute(text(
                "SELECT 1 FROM print_files WHERE file_hash = :h AND blob_state = 'present' LIMIT 1"),
                {"h": entry.name}).first()
            if alive is None:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
    return removed
