"""Protected-input and target-address policy for hardware certification."""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SecurityError(ValueError):
    """Raised before network activity when a safety invariant is violated."""


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_ARTIFACT_ROOT = REPOSITORY_ROOT / "artifacts"


@dataclass(frozen=True)
class SensitiveValueRegistry:
    """Run-local values that must never cross into retained evidence or CLI output."""

    values: tuple[str, ...]

    @classmethod
    def from_target(cls, target: dict[str, Any], *additional: object) -> "SensitiveValueRegistry":
        collected: set[str] = set()

        def visit(value: object) -> None:
            if isinstance(value, dict):
                for nested in value.values():
                    visit(nested)
            elif isinstance(value, (list, tuple)):
                for nested in value:
                    visit(nested)
            elif isinstance(value, str) and len(value) >= 3:
                collected.add(value)

        visit(target)
        visit(additional)
        return cls(tuple(sorted(collected, key=len, reverse=True)))

    def redact(self, value: object) -> str:
        rendered = str(value)
        for sensitive in self.values:
            rendered = rendered.replace(sensitive, "[REDACTED]")
        return rendered


def assert_outside_artifact_trees(path: Path, *additional_roots: Path) -> Path:
    """Resolve an input path and reject every ODIN evidence tree, not one subdirectory."""
    resolved = Path(path).resolve(strict=False)
    roots = {CANONICAL_ARTIFACT_ROOT.resolve()}
    roots.update(Path(root).resolve() for root in additional_roots)
    for root in roots:
        if resolved == root or root in resolved.parents:
            raise SecurityError("protected input may not be stored in an artifact tree")
    return resolved


def _assert_safe_parent(path: Path) -> None:
    parent = path.parent.resolve()
    while True:
        mode = stat.S_IMODE(parent.stat().st_mode)
        if mode & 0o022:
            raise SecurityError("protected JSON parent must not be group/world writable")
        if parent == parent.parent:
            return
        parent = parent.parent


def assert_protected_output_path(path: Path, *additional_roots: Path) -> Path:
    """Validate a new protected-file destination before any content is written."""
    path = Path(path)
    resolved = assert_outside_artifact_trees(path, *additional_roots)
    _assert_safe_parent(path)
    if path.exists() or path.is_symlink():
        raise SecurityError("protected output already exists")
    return resolved


def load_protected_json(path: Path) -> dict[str, Any]:
    """Read a current-user-owned, nonsymlink, mode-0400/0600 JSON object."""
    path = Path(path)
    _assert_safe_parent(path)
    try:
        if stat.S_ISLNK(path.lstat().st_mode):
            raise SecurityError("protected JSON may not be a symlink")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except SecurityError:
        raise
    except OSError as exc:
        raise SecurityError("protected JSON is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SecurityError("protected JSON must be a regular file")
        if metadata.st_uid != os.geteuid():
            raise SecurityError("protected JSON must be owned by the current user")
        if stat.S_IMODE(metadata.st_mode) not in {0o400, 0o600}:
            raise SecurityError("protected JSON mode must be 0400 or 0600")
        if metadata.st_size > 65_536:
            raise SecurityError("protected JSON exceeds size limit")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            payload = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SecurityError("protected JSON is invalid") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(payload, dict):
        raise SecurityError("protected JSON root must be an object")
    return payload


def resolve_private_target(host: str, port: int) -> str:
    """Resolve one exact local/private unicast address or fail closed."""
    if not isinstance(host, str) or not host or len(host) > 253:
        raise SecurityError("target host is invalid")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise SecurityError("target port is invalid")
    if host.lower().endswith(".onion"):
        raise SecurityError("target must resolve to a private address")
    try:
        rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SecurityError("target resolution failed") from exc
    addresses = {row[4][0].split("%", 1)[0] for row in rows}
    if len(addresses) != 1:
        raise SecurityError("target must resolve to exactly one address")
    address = next(iter(addresses))
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError as exc:
        raise SecurityError("resolved target is invalid") from exc
    ipv4_allowlist = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("169.254.0.0/16"),
    )
    ipv6_allowlist = (
        ipaddress.ip_network("fc00::/7"),
        ipaddress.ip_network("::1/128"),
        ipaddress.ip_network("fe80::/10"),
    )
    networks = ipv4_allowlist if isinstance(parsed, ipaddress.IPv4Address) else ipv6_allowlist
    if not any(parsed in network for network in networks):
        raise SecurityError("target must resolve to a private address")
    if isinstance(parsed, ipaddress.IPv4Address):
        octets = address.split(".")
        if address == "255.255.255.255" or octets[-1] in {"0", "255"}:
            raise SecurityError("target must resolve to a private unicast address")
    return address


def canonical_json_sha256(payload: dict[str, Any]) -> str:
    import hashlib

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def target_correlation_sha256(key: str) -> str:
    """Derive a rotatable, non-hardware correlation tag from a protected random key."""
    import hashlib

    if not isinstance(key, str) or len(key) != 64 or any(char not in "0123456789abcdef" for char in key):
        raise SecurityError("evidence correlation key is invalid")
    return hashlib.sha256(b"ODIN-HARDWARE-CORRELATION-V1\0" + bytes.fromhex(key)).hexdigest()
