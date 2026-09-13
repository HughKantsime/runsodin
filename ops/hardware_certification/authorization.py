"""One-time, exact-action authorization for active certification."""

from __future__ import annotations

import re
import secrets
import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .config import validate_target_shape
from .security import (
    CANONICAL_ARTIFACT_ROOT, SecurityError, assert_outside_artifact_trees,
    canonical_json_sha256, load_protected_json,
)


class AuthorizationError(ValueError):
    """Raised before connecting when active authorization is not exact."""


class AuthorizationConsumedError(AuthorizationError):
    """A nonce was durably consumed, but the authorization handoff did not finish."""

    def __init__(self, authorization: dict[str, Any], target: dict[str, Any]):
        super().__init__("authorization handoff failed after nonce consumption")
        self.authorization = authorization
        self.target = target


RUN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{7,12}$")
ACTION_TABLE = {
    "bambu": frozenset({"upload", "start", "pause", "resume", "stop", "ams_read"}),
    "moonraker": frozenset({"upload", "start", "pause", "resume", "cancel"}),
    "prusalink": frozenset({"upload_start", "pause", "resume", "stop"}),
    "elegoo": frozenset({"pause", "resume", "stop"}),
}
AUTH_SCHEMA_PATH = Path(__file__).with_name("schemas") / "authorization.schema.json"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise AuthorizationError("authorization time must be timezone aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _validate_target_shape(target: dict[str, Any]) -> None:
    try:
        validate_target_shape(target)
    except ValueError as exc:
        raise AuthorizationError("target config fields are invalid") from exc
    protocol = target.get("protocol")
    if protocol not in ACTION_TABLE:
        raise AuthorizationError("target protocol is invalid")
    if target.get("schema_version") != 1 or not isinstance(target.get("connection"), dict):
        raise AuthorizationError("target config shape is invalid")
    for name in ("target_alias", "model_family"):
        value = target.get(name)
        if not isinstance(value, str) or not 1 <= len(value) <= 64:
            raise AuthorizationError(f"target {name} is invalid")


def _validate_actions(protocol: str, actions: list[str]) -> None:
    if not actions or len(actions) != len(set(actions)):
        raise AuthorizationError("actions must be a nonempty unique list")
    if any(action not in ACTION_TABLE[protocol] for action in actions):
        raise AuthorizationError("action is not allowed for protocol")
    if protocol == "prusalink" and ({"upload", "start"} & set(actions)):
        raise AuthorizationError("PrusaLink permits only atomic upload_start")


def create_template(
    run_id: str,
    target_path: Path,
    actions: list[str],
    *,
    now: datetime | None = None,
    test_asset_sha256: str | None = None,
    elegoo_filename: str | None = None,
) -> dict[str, Any]:
    if not RUN_ID_RE.fullmatch(run_id):
        raise AuthorizationError("run ID is invalid")
    target = load_protected_json(target_path)
    _validate_target_shape(target)
    protocol = str(target["protocol"])
    _validate_actions(protocol, actions)
    current = _utc(now or datetime.now(timezone.utc))
    requires_asset = bool(set(actions) & {"upload", "start", "upload_start"})
    if requires_asset and (not isinstance(test_asset_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", test_asset_sha256)):
        raise AuthorizationError("active upload/start requires an exact test asset SHA-256")
    if protocol == "elegoo" and not elegoo_filename:
        raise AuthorizationError("Elegoo active actions require an exact preauthorized filename")
    authorization = {
        "schema_version": 1,
        "nonce": secrets.token_hex(16),
        "challenge": secrets.token_hex(12),
        "expires_at": _iso(current + timedelta(minutes=15)),
        "run_id": run_id,
        "target_config_sha256": canonical_json_sha256(target),
        "protocol": protocol,
        "target_alias": target["target_alias"],
        "authorization_state": "DRAFT",
        "operator_confirmation": "",
        "operator_name": "",
        "physical_area_clear": False,
        "disposable_job_confirmed": False,
        "emergency_stop_ready": False,
        "actions": [{"name": action, "approved": False} for action in actions],
    }
    if test_asset_sha256:
        authorization["test_asset_sha256"] = test_asset_sha256
    if elegoo_filename:
        if len(elegoo_filename) > 255 or "/" in elegoo_filename or "\\" in elegoo_filename:
            raise AuthorizationError("Elegoo filename is invalid")
        salt = secrets.token_hex(16)
        import hashlib

        authorization["elegoo_filename_salt"] = salt
        authorization["elegoo_filename_sha256"] = hashlib.sha256((salt + elegoo_filename).encode("utf-8")).hexdigest()
    return authorization


def validate_authorization(
    authorization: dict[str, Any],
    *,
    target_path: Path,
    validated_target: dict[str, Any] | None = None,
    now: datetime | None = None,
    expected_commit: str | None = None,
) -> dict[str, Any]:
    required_fields = {
        "schema_version", "nonce", "challenge", "expires_at", "run_id",
        "target_config_sha256", "protocol", "target_alias", "authorization_state",
        "operator_confirmation", "operator_name", "physical_area_clear",
        "disposable_job_confirmed", "emergency_stop_ready", "actions",
    }
    optional_fields = {"test_asset_sha256", "elegoo_filename_salt", "elegoo_filename_sha256"}
    if not required_fields <= set(authorization) or set(authorization) - required_fields - optional_fields:
        raise AuthorizationError("authorization fields are invalid")
    schema_errors = list(Draft202012Validator(json.loads(AUTH_SCHEMA_PATH.read_text(encoding="utf-8"))).iter_errors(authorization))
    if schema_errors:
        raise AuthorizationError("authorization schema is invalid")
    target = validated_target if validated_target is not None else load_protected_json(target_path)
    _validate_target_shape(target)
    if authorization.get("schema_version") != 1:
        raise AuthorizationError("authorization schema version is invalid")
    if authorization.get("target_config_sha256") != canonical_json_sha256(target):
        raise AuthorizationError("authorization target binding is invalid")
    if authorization.get("protocol") != target["protocol"] or authorization.get("target_alias") != target["target_alias"]:
        raise AuthorizationError("authorization target is mismatched")
    if authorization.get("authorization_state") != "AUTHORIZED":
        raise AuthorizationError("authorization is not explicitly authorized")
    challenge = authorization.get("challenge")
    if not isinstance(challenge, str) or not re.fullmatch(r"[0-9a-f]{24}", challenge):
        raise AuthorizationError("authorization challenge is invalid")
    if authorization.get("operator_confirmation") != challenge:
        raise AuthorizationError("operator confirmation does not match challenge")
    operator = authorization.get("operator_name")
    if not isinstance(operator, str) or not operator.strip() or len(operator) > 80:
        raise AuthorizationError("operator name is required")
    for field in ("physical_area_clear", "disposable_job_confirmed", "emergency_stop_ready"):
        if authorization.get(field) is not True:
            raise AuthorizationError(f"{field} acknowledgement is required")
    actions = authorization.get("actions")
    if not isinstance(actions, list) or not all(isinstance(item, dict) and set(item) == {"name", "approved"} for item in actions):
        raise AuthorizationError("authorization actions are invalid")
    names = [item["name"] for item in actions]
    _validate_actions(str(authorization["protocol"]), names)
    if set(names) & {"upload", "start", "upload_start"}:
        if not re.fullmatch(r"[0-9a-f]{64}", str(authorization.get("test_asset_sha256", ""))):
            raise AuthorizationError("active upload/start requires a test asset SHA-256")
    if authorization["protocol"] == "elegoo":
        if not re.fullmatch(r"[0-9a-f]{32}", str(authorization.get("elegoo_filename_salt", ""))):
            raise AuthorizationError("Elegoo filename authorization is missing")
        if not re.fullmatch(r"[0-9a-f]{64}", str(authorization.get("elegoo_filename_sha256", ""))):
            raise AuthorizationError("Elegoo filename authorization is missing")
    if any(item["approved"] is not True for item in actions):
        raise AuthorizationError("every action requires explicit approval")
    try:
        expiry = datetime.fromisoformat(str(authorization["expires_at"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuthorizationError("authorization expiry is invalid") from exc
    current = _utc(now or datetime.now(timezone.utc))
    expiry = _utc(expiry)
    if current >= expiry:
        raise AuthorizationError("authorization has expired")
    if expiry - current > timedelta(minutes=15):
        raise AuthorizationError("authorization expiry exceeds maximum window")
    if not RUN_ID_RE.fullmatch(str(authorization.get("run_id", ""))):
        raise AuthorizationError("authorization run ID is invalid")
    if expected_commit and authorization["run_id"].rsplit("-", 1)[-1] != expected_commit:
        raise AuthorizationError("authorization run commit is mismatched")
    return authorization


def consume_authorization(
    authorization_path: Path,
    *,
    target_path: Path,
    ledger_path: Path,
    now: datetime | None = None,
    expected_commit: str | None = None,
    artifact_root: Path = CANONICAL_ARTIFACT_ROOT,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate and consume authorization once before any active connection."""
    try:
        assert_outside_artifact_trees(authorization_path, artifact_root)
        assert_outside_artifact_trees(target_path, artifact_root)
        assert_outside_artifact_trees(ledger_path, artifact_root)
    except SecurityError as exc:
        raise AuthorizationError("authorization inputs must be outside artifact trees") from exc
    authorization = load_protected_json(authorization_path)
    target = load_protected_json(target_path)
    validate_authorization(
        authorization, target_path=target_path, now=now,
        expected_commit=expected_commit, validated_target=target,
    )
    nonce = authorization["nonce"]
    ledger_path = Path(ledger_path)
    ledger_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent_metadata = ledger_path.parent.lstat()
    if (
        stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(parent_metadata.st_mode) != 0o700
    ):
        raise AuthorizationError("authorization ledger directory must be current-user mode 0700")
    try:
        existing = ledger_path.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None and stat.S_ISLNK(existing.st_mode):
        raise AuthorizationError("authorization ledger may not be a symlink")
    flags = (
        os.O_RDWR | os.O_CREAT | os.O_APPEND
        | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(ledger_path, flags, 0o600)
    except OSError as exc:
        raise AuthorizationError("authorization ledger is unavailable") from exc
    recorded = False
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise AuthorizationError("authorization ledger must be a current-user regular file")
        if existing is None:
            os.fchmod(descriptor, 0o600)
        elif stat.S_IMODE(metadata.st_mode) != 0o600:
            raise AuthorizationError("authorization ledger mode must be 0600")
        if metadata.st_size > 1_048_576:
            raise AuthorizationError("authorization ledger exceeds size limit")
        with os.fdopen(descriptor, "a+", encoding="utf-8") as ledger:
            descriptor = -1
            import fcntl

            fcntl.flock(ledger.fileno(), fcntl.LOCK_EX)
            ledger.seek(0)
            used: set[str] = set()
            current = _utc(now or datetime.now(timezone.utc))
            for raw_line in ledger:
                try:
                    record = json.loads(raw_line)
                    if set(record) != {"nonce", "consumed_at"} or not re.fullmatch(
                        r"[0-9a-f]{32}", str(record["nonce"])
                    ):
                        raise ValueError
                    consumed_at = _utc(datetime.fromisoformat(
                        str(record["consumed_at"]).replace("Z", "+00:00")
                    ))
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise AuthorizationError("authorization ledger is malformed") from exc
                if consumed_at > current:
                    raise AuthorizationError("authorization ledger detects clock rollback")
                used.add(str(record["nonce"]))
            if nonce in used:
                raise AuthorizationError("authorization nonce was already consumed")
            ledger.seek(0, os.SEEK_END)
            ledger.write(json.dumps({
                "nonce": nonce, "consumed_at": _iso(current),
            }, sort_keys=True, separators=(",", ":")) + "\n")
            ledger.flush()
            os.fsync(ledger.fileno())
            recorded = True
    except BaseException as exc:
        if recorded:
            raise AuthorizationConsumedError(authorization, target) from exc
        raise
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
    try:
        Path(authorization_path).unlink()
    except BaseException as exc:
        raise AuthorizationConsumedError(authorization, target) from exc
    return authorization, target
