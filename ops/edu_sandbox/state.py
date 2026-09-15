"""Versioned state model and legal Education sandbox transitions."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from .errors import StateError, ValidationError
from .identity import assert_path_owned, validate_sandbox_id

STATE_SCHEMA_VERSION = 1


class Phase(str, Enum):
    PREPARING = "PREPARING"
    PREPARED = "PREPARED"
    REQUESTING_LICENSE = "REQUESTING_LICENSE"
    ACTIVATING = "ACTIVATING"
    ACTIVE = "ACTIVE"
    RESETTING = "RESETTING"
    EXPIRED = "EXPIRED"
    DEGRADED = "DEGRADED"
    PURGING = "PURGING"
    PURGED = "PURGED"


class ObservedStatus(str, Enum):
    ABSENT = "ABSENT"
    STARTING = "STARTING"
    READY = "READY"
    STOPPED = "STOPPED"
    EXPIRED = "EXPIRED"
    DEGRADED = "DEGRADED"
    PURGED = "PURGED"


_LEGAL: dict[Phase | None, frozenset[Phase]] = {
    None: frozenset({Phase.PREPARING}),
    Phase.PREPARING: frozenset({Phase.PREPARED, Phase.DEGRADED, Phase.PURGING}),
    Phase.PREPARED: frozenset({Phase.REQUESTING_LICENSE, Phase.ACTIVATING, Phase.PURGING}),
    Phase.REQUESTING_LICENSE: frozenset({Phase.PREPARED, Phase.DEGRADED, Phase.PURGING}),
    Phase.ACTIVATING: frozenset({Phase.ACTIVE, Phase.PREPARED, Phase.EXPIRED, Phase.DEGRADED, Phase.PURGING}),
    Phase.ACTIVE: frozenset({Phase.RESETTING, Phase.EXPIRED, Phase.DEGRADED, Phase.PURGING}),
    Phase.RESETTING: frozenset({Phase.ACTIVE, Phase.DEGRADED, Phase.PURGING}),
    Phase.EXPIRED: frozenset({Phase.ACTIVATING, Phase.DEGRADED, Phase.PURGING}),
    Phase.DEGRADED: frozenset({Phase.RESETTING, Phase.PURGING}),
    Phase.PURGING: frozenset({Phase.PURGED, Phase.DEGRADED}),
    Phase.PURGED: frozenset(),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class LifecycleState:
    sandbox_id: str
    phase: Phase
    schema_version: int = STATE_SCHEMA_VERSION
    source_commit: str = ""
    source_dirty: bool = False
    candidate_tag: str = ""
    candidate_image_id: str = ""
    image_ownership_journal_sha256: str = ""
    broker_image_id: str = ""
    broker_digest: str = ""
    compose_project: str = ""
    loopback_port: int | None = None
    installation_id: str = ""
    device_public_key_sha256: str = ""
    license_sha256: str = ""
    license_tier: str = ""
    license_expires_at: str = ""
    binding_present: bool = False
    binding_matches_current: bool = False
    lease_starts_at: str = ""
    lease_expires_at: str = ""
    reset_generation: int = 0
    resources: dict[str, Any] = field(default_factory=dict)
    verification: dict[str, Any] = field(default_factory=dict)
    last_transition: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_sandbox_id(self.sandbox_id)
        if self.schema_version != STATE_SCHEMA_VERSION:
            raise ValidationError(f"unsupported state schema: {self.schema_version}")
        if isinstance(self.phase, str):
            self.phase = Phase(self.phase)

    def transition(self, target: Phase, *, detail: str) -> None:
        if target not in _LEGAL[self.phase]:
            raise StateError(f"illegal transition: {self.phase.value} -> {target.value}")
        previous = self.phase
        self.phase = target
        self.last_transition = {
            "from": previous.value,
            "to": target.value,
            "at": utc_now(),
            "detail": detail,
        }

    def to_public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["phase"] = self.phase.value
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "LifecycleState":
        allowed = set(cls.__dataclass_fields__)
        unexpected = sorted(set(value) - allowed)
        if unexpected:
            raise ValidationError("state contains unexpected fields: " + ", ".join(unexpected))
        return cls(**value)


def load_state(path: Path) -> LifecycleState:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise StateError("sandbox state does not exist") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError("sandbox state is unreadable") from exc
    if not isinstance(value, dict):
        raise ValidationError("state document must be a JSON object")
    return LifecycleState.from_dict(value)


def save_state(path: Path, state: LifecycleState, sandbox_dir: Path) -> None:
    destination = assert_path_owned(path, sandbox_dir)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- lifecycle state directories require owner-only read/write/traverse; 0644 is invalid for a directory
    os.chmod(destination.parent, 0o700)
    payload = json.dumps(state.to_public_dict(), indent=2, sort_keys=True) + "\n"
    fd, temporary_name = tempfile.mkstemp(prefix=".state-", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
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
