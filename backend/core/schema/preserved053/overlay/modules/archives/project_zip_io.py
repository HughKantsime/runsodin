"""Project ZIP v2 export packaging and import file staging.

Export builds a closed v2 manifest (see project_zip_manifest) and only reads print
files that live under the print-file storage root. Import writes verified bytes into
a fresh private directory under that root; callers own the database transaction and
must call ``discard_import_dir`` if it does not commit.
"""

import hashlib
import io
import json
import os
import shutil
import stat
import uuid
import zipfile
from datetime import datetime, timezone

from pydantic import ValidationError

from modules.archives.project_zip_admission import MAX_COMPRESSED_BYTES
from modules.archives.project_zip_manifest import (
    MAX_FILE_BYTES,
    MAX_METADATA_BYTES,
    MAX_TOTAL_BYTES,
    ProjectZipManifest,
)

TERMINAL_STATUSES = ("completed", "failed", "cancelled")
EXPORT_EXTENSIONS = (".3mf", ".gcode", ".bgcode")
DEFAULT_COLOR = "#6366f1"
IMPORT_DIRNAME = "project-imports"


class ProjectZipExportError(ValueError):
    """The project holds data that cannot be represented in a v2 package."""


def storage_root():
    configured = os.environ.get("PRINT_FILE_STORAGE_ROOT") or "/data/print_files"
    if not os.path.isabs(configured):
        raise RuntimeError("PRINT_FILE_STORAGE_ROOT must be an absolute path")
    return os.path.realpath(configured)


def _utc(value):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tags(value):
    if not value:
        return []
    return [tag.strip() for tag in str(value).split(",") if tag.strip()]


def _read_stored_file(path, remaining):
    """Return bytes of a regular file under the storage root, or None if not exportable.

    Symlinks are resolved first and the target must stay inside the root; O_NOFOLLOW
    then refuses a final component swapped for a link after resolution. Raises
    ProjectZipExportError when the file would exceed the remaining package budget.
    """
    if not path or not str(path).lower().endswith(EXPORT_EXTENSIONS):
        return None
    root = storage_root()
    real = os.path.realpath(path)
    if os.path.commonpath([root, real]) != root or real == root:
        return None
    try:
        fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            return None
        if info.st_size > remaining:
            raise ProjectZipExportError("Project cannot be exported: archive files exceed the 512 MiB package limit")
        with os.fdopen(fd, "rb") as handle:
            fd = None
            data = handle.read(MAX_FILE_BYTES + 1)
    finally:
        if fd is not None:
            os.close(fd)
    return data if len(data) <= MAX_FILE_BYTES else None


def build_project_zip(project, archives):
    """Build v2 ZIP bytes for a project row mapping and its visible archive mappings."""
    entries = []
    blobs = {}
    remaining = MAX_TOTAL_BYTES
    for archive in archives:
        if archive.get("status") not in TERMINAL_STATUSES:
            continue
        index = len(entries)
        # education_linked carries the full generic-audience rule evaluated in SQL
        # (direct marker, linked submission print file/job, or upload operation path).
        education = archive.get("education_submission_id") is not None or bool(archive.get("education_linked"))
        file_entry = None
        data = None if education else _read_stored_file(archive.get("file_path"), remaining)
        if data is not None:
            remaining -= len(data)
            original = os.path.basename(archive["file_path"])
            member = f"files/{index:03d}{os.path.splitext(original)[1].lower()}"
            file_entry = {
                "member": member,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "original_name": original,
            }
            blobs[member] = data
        duration = archive.get("actual_duration_seconds")
        grams = archive.get("filament_used_grams")
        entries.append({
            "archive_key": str(index),
            "display": {
                "print_name": archive.get("print_name") or "Unknown",
                "notes": archive.get("notes"),
                "tags": _tags(archive.get("tags")),
                "status": archive["status"],
                "printed_at": _utc(archive.get("completed_at") or archive.get("created_at")),
                "actual_print_seconds": float(duration) if duration is not None else None,
                "filament_used_g": float(grams) if grams is not None else None,
            },
            "protection": "education" if education else "generic",
            "file": file_entry,
        })

    manifest = {
        "version": 2,
        "project": {
            "name": project.get("name") or "Project",
            "description": project.get("description"),
            "color": project.get("color") or DEFAULT_COLOR,
            "expected_parts": project.get("expected_parts"),
        },
        "archives": entries,
    }
    try:
        ProjectZipManifest.model_validate(manifest)
    except ValidationError as exc:
        # Fail loudly rather than emit a package that import would refuse.
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"])
        raise ProjectZipExportError(f"Project cannot be exported: {location}: {first['msg']}") from None

    metadata = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(metadata) > MAX_METADATA_BYTES:
        raise ProjectZipExportError("Project cannot be exported: metadata exceeds the 1 MiB limit")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("project.json", metadata)
        for member, data in blobs.items():
            zf.writestr(member, data)
    payload = buf.getvalue()
    if len(payload) > MAX_COMPRESSED_BYTES:
        raise ProjectZipExportError("Project cannot be exported: package exceeds the 256 MiB import limit")
    return payload


def _write_private(directory_fd, name, data):
    tmp = f".{name}.{uuid.uuid4().hex}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rename(tmp, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)


def stage_import_files(payload, manifest):
    """Write verified archive files to a new private directory.

    Returns ``(import_dir, {archive_key: (path, sha256)})``. The package must already
    have passed ``validate_project_zip``; hashes are re-checked while writing.
    """
    root = storage_root()
    parent = os.path.join(root, IMPORT_DIRNAME)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    import_dir = os.path.join(parent, uuid.uuid4().hex)
    os.mkdir(import_dir, 0o700)
    placed = {}
    try:
        directory_fd = os.open(import_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as package:
                for archive in manifest.archives:
                    if archive.file is None:
                        continue
                    data = package.read(archive.file.member)
                    digest = hashlib.sha256(data).hexdigest()
                    if len(data) != archive.file.size or digest != archive.file.sha256:
                        raise ValueError("Invalid or oversized project ZIP")
                    name = os.path.basename(archive.file.member)
                    extension = os.path.splitext(name)[1]
                    if archive.file.original_name.lower().endswith(extension):
                        # Keep the user-visible filename; the index prefix keeps names unique.
                        name = name[: -len(extension)] + "-" + archive.file.original_name
                    _write_private(directory_fd, name, data)
                    placed[archive.archive_key] = (os.path.join(import_dir, name), digest)
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        discard_import_dir(import_dir)
        raise
    return import_dir, placed


def discard_import_dir(import_dir):
    root = os.path.join(storage_root(), IMPORT_DIRNAME)
    if import_dir and os.path.dirname(os.path.realpath(import_dir)) == os.path.realpath(root):
        shutil.rmtree(import_dir, ignore_errors=True)
