"""Bounded secret ingestion and controller-owned secret files."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
from pathlib import Path
from typing import BinaryIO

from ops.release_gate.policy import redact_text, scan_text_for_secrets

from .errors import SecretInputError
from .identity import assert_path_owned

REQUEST_LICENSE_LIMIT = 64 * 1024
LICENSE_LIMIT = 1024 * 1024
ACTIVATION_REQUEST_FILENAME = "activation-request.json"
LICENSE_FILENAME = "license.json"
PRIOR_LICENSE_FILENAME = "license.previous"


def read_bounded_stdin(stream: BinaryIO, *, limit: int, is_tty: bool) -> bytes:
    if is_tty:
        raise SecretInputError("secret input requires a non-TTY stdin pipe")
    if limit <= 0:
        raise ValueError("limit must be positive")
    content = stream.read(limit + 1)
    if len(content) > limit:
        raise SecretInputError(f"secret input exceeds {limit} bytes")
    if not content.strip():
        raise SecretInputError("secret input is empty")
    return content


def read_json_object(stream: BinaryIO, *, limit: int, is_tty: bool) -> dict[str, object]:
    content = read_bounded_stdin(stream, limit=limit, is_tty=is_tty)
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecretInputError("secret input must be valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise SecretInputError("secret input must be a JSON object")
    return value


def write_secret(path: Path, content: bytes, sandbox_dir: Path) -> str:
    sandbox = sandbox_dir.resolve(strict=False)
    destination = Path(os.path.abspath(path))
    try:
        relative = destination.relative_to(sandbox)
    except ValueError as exc:
        raise SecretInputError("secret path is outside the validated sandbox directory") from exc
    if not relative.parts:
        raise SecretInputError("secret path must name a file inside the sandbox")

    # Do not allow an existing symlink at any lifecycle-controlled component.
    # Resolving the destination first would hide a link whose target happens to
    # remain inside the sandbox and would make O_TRUNC write through that link.
    current = sandbox
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise SecretInputError("secret path contains a symlink")

    assert_path_owned(destination, sandbox)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    current = sandbox
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise SecretInputError("secret path contains a symlink")
    # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- secret directories require owner-only read/write/traverse; 0644 is invalid and less secure for a directory
    os.chmod(destination.parent, 0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=".secret-", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if destination.is_symlink():
            raise SecretInputError("secret destination became a symlink")
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(destination.parent, directory_flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    return hashlib.sha256(content).hexdigest()


def sanitized_text(text: str, known_secrets: list[str]) -> str:
    redacted = redact_text(text, known_secrets)
    findings = scan_text_for_secrets(redacted, known_secrets)
    if findings:
        raise SecretInputError("retained output failed secret scan: " + ", ".join(findings))
    return redacted


def bytes_stream(value: bytes) -> BinaryIO:
    """Small test/helper adapter with the same interface as stdin.buffer."""
    return io.BytesIO(value)
