"""Validation for operator-supplied disposable certification assets."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .security import CANONICAL_ARTIFACT_ROOT, SecurityError


MAX_ASSET_BYTES = 256 * 1024 * 1024
EXTENSIONS = {
    "bambu": frozenset({".3mf"}),
    "moonraker": frozenset({".gcode"}),
    "prusalink": frozenset({".gcode"}),
}


@dataclass(frozen=True)
class ValidatedAsset:
    path: Path
    extension: str
    sha256: str
    size: int
    device: int
    inode: int

    def open(self) -> BinaryIO:
        """Reopen the same protected inode or fail before reading any bytes."""
        try:
            descriptor = os.open(
                self.path,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            metadata = os.fstat(descriptor)
            if (
                metadata.st_dev != self.device or metadata.st_ino != self.inode
                or metadata.st_size != self.size or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) not in {0o400, 0o600}
            ):
                raise SecurityError("test asset identity changed after authorization")
            with os.fdopen(os.dup(descriptor), "rb") as verifier:
                if hashlib.file_digest(verifier, "sha256").hexdigest() != self.sha256:
                    raise SecurityError("test asset content changed after authorization")
            os.lseek(descriptor, 0, os.SEEK_SET)
            return os.fdopen(descriptor, "rb")
        except Exception:
            if "descriptor" in locals():
                os.close(descriptor)
            raise


def _validated_asset(
    path: Path, *, protocol: str, repository_root: Path, artifact_root: Path,
) -> ValidatedAsset:
    candidate = Path(path)
    try:
        if stat.S_ISLNK(candidate.lstat().st_mode):
            raise SecurityError("test asset must be a regular nonsymlink file")
        descriptor = os.open(
            candidate,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except SecurityError:
        raise
    except OSError as exc:
        raise SecurityError("test asset is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SecurityError("test asset must be a regular nonsymlink file")
        if metadata.st_uid != os.geteuid():
            raise SecurityError("test asset must be owned by the current user")
        if stat.S_IMODE(metadata.st_mode) not in {0o400, 0o600}:
            raise SecurityError("test asset mode must be 0400 or 0600")
        if not 0 < metadata.st_size <= MAX_ASSET_BYTES:
            raise SecurityError("test asset size is outside certification bounds")
        resolved = candidate.resolve(strict=True)
        resolved_metadata = resolved.stat()
        if (resolved_metadata.st_dev, resolved_metadata.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise SecurityError("test asset identity changed during validation")
        for forbidden in (
            Path(repository_root).resolve(), CANONICAL_ARTIFACT_ROOT.resolve(),
            Path(artifact_root).resolve(),
        ):
            if resolved == forbidden or forbidden in resolved.parents:
                raise SecurityError("test asset must be outside repository and artifact trees")
        extension = resolved.suffix.lower()
        if extension not in EXTENSIONS.get(protocol, frozenset()):
            raise SecurityError("test asset extension is not supported for protocol")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        return ValidatedAsset(
            resolved, extension, digest, metadata.st_size, metadata.st_dev, metadata.st_ino,
        )
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def inspect_test_asset(
    path: Path, *, protocol: str, repository_root: Path, artifact_root: Path,
) -> tuple[Path, str, str]:
    """Return resolved path, extension, and digest for a protected asset."""
    asset = _validated_asset(
        path, protocol=protocol, repository_root=repository_root, artifact_root=artifact_root,
    )
    return asset.path, asset.extension, asset.sha256


def validate_test_asset(
    path: Path, *, protocol: str, expected_sha256: str,
    repository_root: Path, artifact_root: Path,
) -> tuple[ValidatedAsset, str]:
    """Return a resolved safe asset and its extension, or fail before I/O."""
    asset = _validated_asset(
        path, protocol=protocol, repository_root=repository_root,
        artifact_root=artifact_root,
    )
    if asset.sha256 != expected_sha256:
        raise SecurityError("test asset SHA-256 does not match authorization")
    return asset, asset.extension
