"""Docker-backed ODIN Education sandbox lifecycle operations."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets as random_secrets
import socket
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timezone
from http.client import HTTPConnection
from pathlib import Path

from cryptography.fernet import Fernet

from ops.release_control.validation_image_cleanup import (
    DisposableImageLifecycle,
    PROBE_IMAGE_ID,
    ProtectedImageHistory,
    VALIDATION_CAPACITY_COMMAND,
    VALIDATION_DOCKER_MIN_FREE_KIB,
    acquire_image_lifecycle_lock,
    cleanup_owned_image_history,
    inspect_image_id,
    read_iidfile,
    release_image_lifecycle_lock,
    remove_owned_image,
    verify_owned_image_history_absent,
    verify_built_image,
)

from .errors import OwnershipError, SandboxError, SecretInputError, StateError, ValidationError
from .executor import Executor
from .identity import compose_project, sandbox_directory, validate_sandbox_id, validate_state_root
from .secrets import (
    ACTIVATION_REQUEST_FILENAME,
    LICENSE_FILENAME,
    LICENSE_LIMIT,
    PRIOR_LICENSE_FILENAME,
    REQUEST_LICENSE_LIMIT,
    read_bounded_stdin,
    read_json_object,
    write_secret,
)
from .state import LifecycleState, ObservedStatus, Phase, load_state, save_state, utc_now

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = Path(__file__).with_name("docker-compose.yml")
DEFAULT_STATE_ROOT = ROOT / ".odin-edu-sandboxes"
OWNER_LABEL = "com.runsodin.edu-sandbox"
SCHEMA_LABEL = "com.runsodin.edu-schema"
GENERATION_LABEL = "com.runsodin.edu-generation"
PROJECT_LABEL = "com.docker.compose.project"
BROKER_IMAGE = "eclipse-mosquitto@sha256:914f529386804c8278a4e581526b9be5e1604df44b30daabc70aa97dcefe5268"

_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_PORT = re.compile(r"127\.0\.0\.1:(\d+)$")
_HELPER_PURPOSE = re.compile(r"^[a-z0-9-]{2,24}$")


def _helper_container_name(project: str, purpose: str) -> str:
    if not project.startswith("odin-edu-") or not _HELPER_PURPOSE.fullmatch(purpose):
        raise ValueError("invalid sandbox helper identity")
    return f"{project}-helper-{purpose}-{random_secrets.token_hex(4)}"


@dataclass(frozen=True)
class SandboxPaths:
    root: Path
    directory: Path
    state: Path
    secrets: Path
    public: Path
    image_journal: Path
    image_iid: Path

    @classmethod
    def create(cls, state_root: Path, sandbox_id: str) -> "SandboxPaths":
        root = validate_state_root(state_root)
        directory = sandbox_directory(root, sandbox_id)
        return cls(
            root,
            directory,
            directory / "state.json",
            directory / "secrets",
            directory / "public",
            directory / "image-ownership.json",
            directory / "image.iid",
        )


@dataclass(frozen=True)
class ResourceSet:
    project: str
    internal_network: str
    edge_network: str
    data_volume: str
    secret_volume: str
    heartbeat_volume: str
    prepare_container: str
    prepare_proxy: str

    @classmethod
    def from_id(cls, sandbox_id: str) -> "ResourceSet":
        project = compose_project(sandbox_id)
        return cls(
            project=project,
            internal_network=f"{project}-internal",
            edge_network=f"{project}-edge",
            data_volume=f"{project}-data",
            secret_volume=f"{project}-secrets",
            heartbeat_volume=f"{project}-heartbeat",
            prepare_container=f"{project}-prepare",
            prepare_proxy=f"{project}-prepare-proxy",
        )

    def state_value(self) -> dict[str, str]:
        return {
            "internal_network": self.internal_network,
            "edge_network": self.edge_network,
            "data_volume": self.data_volume,
            "secret_volume": self.secret_volume,
            "heartbeat_volume": self.heartbeat_volume,
        }


_IDENTITY_SCRIPT = r"""
import base64, hashlib, json
import license_manager as lm
installation_id = lm.get_installation_id()
_private, public = lm.get_device_keypair()
fingerprint = hashlib.sha256(base64.b64decode(public)).hexdigest()
version = open("/app/VERSION", encoding="utf-8").read().strip()
print(json.dumps({"installation_id": installation_id, "device_public_key_sha256": fingerprint, "odin_version": version}))
""".strip()

_READ_IDENTITY_SCRIPT = r"""
import hashlib, json, pathlib, uuid
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
install_path = pathlib.Path("/data/.odin-install-id")
key_path = pathlib.Path("/data/.odin-device.key")
if install_path.is_symlink() or key_path.is_symlink() or not install_path.is_file() or not key_path.is_file():
    raise SystemExit("persisted identity files are missing or unsafe")
installation_id = install_path.read_text(encoding="utf-8").strip()
try:
    uuid.UUID(installation_id)
except ValueError as exc:
    raise SystemExit("persisted installation ID is invalid") from exc
private = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
if not isinstance(private, Ed25519PrivateKey):
    raise SystemExit("persisted device key has the wrong type")
public = private.public_key().public_bytes(
    encoding=serialization.Encoding.Raw,
    format=serialization.PublicFormat.Raw,
)
fingerprint = hashlib.sha256(public).hexdigest()
version = open("/app/VERSION", encoding="utf-8").read().strip()
print(json.dumps({"installation_id": installation_id, "device_public_key_sha256": fingerprint, "odin_version": version}))
""".strip()

_SECRET_VOLUME_SCRIPT = r"""
import json, os, pathlib, sys
target = pathlib.Path("/run/odin-secrets")
target.mkdir(mode=0o700, parents=True, exist_ok=True)
# The entrypoint reads application/persona files as root. The backend later
# reads only license.json as uid/gid 10001, so grant directory traversal to
# that group while leaving every other file root-owned 0600.
os.chown(target, 0, 10001)
os.chmod(target, 0o750)
values = json.load(sys.stdin)
allowed = {"api-key", "jwt-secret", "encryption-key", "admin-password", "teacher-password", "student-password"}
if set(values) != allowed:
    raise SystemExit("unexpected secret bundle")
for name, value in values.items():
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        raise SystemExit("invalid secret value")
    path = target / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(path, 0o600)
""".strip()

_PUT_LICENSE_SCRIPT = r"""
import os, pathlib, sys
content = sys.stdin.buffer.read(1024 * 1024 + 1)
if not content or len(content) > 1024 * 1024:
    raise SystemExit("invalid license length")
target = pathlib.Path("/run/odin-secrets/license.json")
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "wb") as handle:
    handle.write(content)
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(target, 0o600)
os.chown(target, 10001, 10001)
""".strip()

_DELETE_LICENSE_SCRIPT = r"""
import json, pathlib
path = pathlib.Path("/run/odin-secrets/license.json")
if path.exists():
    if path.is_symlink() or not path.is_file():
        raise SystemExit("license target is not a regular file")
    path.unlink()
print(json.dumps({"license_absent": not path.exists()}))
""".strip()

_LICENSE_DIGEST_SCRIPT = r"""
import hashlib, json, pathlib
path = pathlib.Path("/run/odin-secrets/license.json")
if path.is_symlink() or not path.is_file():
    raise SystemExit("license is not a regular file")
if path.stat().st_mode & 0o077:
    raise SystemExit("license permissions are unsafe")
print(json.dumps({"license_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}))
""".strip()

_REQUEST_LICENSE_SCRIPT = r"""
import base64, json, os, pathlib, sys, uuid
from datetime import datetime, timezone
import license_manager as lm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
body = json.load(sys.stdin)
if set(body) != {"key", "nonce"} or not all(isinstance(body[k], str) and body[k] for k in body):
    raise SystemExit("request requires key and nonce")
install_path = pathlib.Path("/data/.odin-install-id")
key_path = pathlib.Path("/data/.odin-device.key")
if install_path.is_symlink() or key_path.is_symlink() or not install_path.is_file() or not key_path.is_file():
    raise SystemExit("persisted identity files are missing or unsafe")
installation_id = install_path.read_text(encoding="utf-8").strip()
try:
    uuid.UUID(installation_id)
except ValueError as exc:
    raise SystemExit("persisted installation ID is invalid") from exc
private = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
if not isinstance(private, Ed25519PrivateKey):
    raise SystemExit("persisted device key has the wrong type")
public = base64.b64encode(private.public_key().public_bytes(
    encoding=serialization.Encoding.Raw,
    format=serialization.PublicFormat.Raw,
)).decode("ascii")
lm.get_device_keypair = lambda: (private, public)
signature = lm.sign_license_challenge("activate-bootstrap", body["key"], installation_id, body["nonce"])
version = open("/app/VERSION", encoding="utf-8").read().strip()
print(json.dumps({"key": body["key"], "activation_request": {
  "installation_id": installation_id,
  "hostname": "odin-edu-" + os.environ["ODIN_EDU_SANDBOX_ID"] + "-app",
  "odin_version": version,
  "generated_at": datetime.now(timezone.utc).isoformat(),
  "device_pubkey": public,
  "nonce": body["nonce"],
  "bootstrap_signature": signature,
}}, separators=(",", ":")))
""".strip()

_VALIDATE_LICENSE_SCRIPT = r"""
import json, os, tempfile, sys
import license_manager as lm
raw = sys.stdin.buffer.read(1024 * 1024 + 1)
if not raw or len(raw) > 1024 * 1024:
    raise SystemExit("invalid license length")
text = raw.decode("utf-8").strip()
try:
    wrapper = json.loads(text)
    if isinstance(wrapper, dict) and set(wrapper) >= {"payload", "signature"}:
        text = wrapper["payload"] + "." + wrapper["signature"]
except json.JSONDecodeError:
    pass
fd, name = tempfile.mkstemp(prefix="odin-license-", text=True)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    lm._find_license_file = lambda: name
    lm.get_installation_id = lambda: os.environ["ODIN_EXPECTED_INSTALLATION_ID"]
    info = lm.load_license()
    print(json.dumps({
      "valid": info.valid,
      "tier": info.tier,
      "expires_at": info.expires_at,
      "expired": info.expired,
      "binding_present": info.binding_present,
      "binding_matches_current": info.binding_matches_current,
      "license_sha256": info.license_sha256,
      "error": info.error,
    }, separators=(",", ":")))
finally:
    os.unlink(name)
""".strip()

_RESET_DATA_SCRIPT = r"""
import os, pathlib
root = pathlib.Path("/data")
preserve = {".odin-install-id", ".odin-device.key"}
for name in preserve:
    path = root / name
    if not path.is_file() or path.is_symlink():
        raise SystemExit("identity invariant failed")
for path in root.rglob("*"):
    if path.is_symlink():
        raise SystemExit("symlink found in mutable data volume")
for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
    if path.parent == root and path.name in preserve:
        continue
    if path.is_dir():
        path.rmdir()
    else:
        path.unlink()
""".strip()

_CREATE_RESET_SENTINEL_SCRIPT = r"""
import json, sqlite3
connection = sqlite3.connect("/data/odin.db")
try:
    with connection:
        connection.execute(
            "INSERT INTO system_config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("edu_reset_sentinel", json.dumps({"synthetic": True})),
        )
    count = connection.execute(
        "SELECT COUNT(*) FROM system_config WHERE key=?", ("edu_reset_sentinel",)
    ).fetchone()[0]
    if count != 1:
        raise SystemExit("reset sentinel was not created")
    print(json.dumps({"reset_sentinel_created": True}))
finally:
    connection.close()
""".strip()

_CHECK_RESET_SENTINEL_SCRIPT = r"""
import json, sqlite3
connection = sqlite3.connect("/data/odin.db")
try:
    count = connection.execute(
        "SELECT COUNT(*) FROM system_config WHERE key=?", ("edu_reset_sentinel",)
    ).fetchone()[0]
    if count:
        raise SystemExit("reset sentinel survived data replacement")
    print(json.dumps({"reset_sentinel_removed": True}))
finally:
    connection.close()
""".strip()

_INERT_LOG_PROOF_SCRIPT = r"""
import json, pathlib
names = ("Northstar Elegoo Inert", "Northstar Moonraker Inert", "Northstar PrusaLink Inert")
paths = (
    pathlib.Path("/data/elegoo_monitor.log"),
    pathlib.Path("/data/moonraker_monitor.log"),
    pathlib.Path("/data/prusalink_monitor.log"),
)
checked = 0
for path in paths:
    if path.is_symlink():
        raise SystemExit("monitor log cannot be a symlink")
    if not path.exists():
        continue
    text = path.read_text(encoding="utf-8", errors="replace")
    checked += 1
    if any(name in text for name in names):
        raise SystemExit("inert printer connection-attempt marker found")
print(json.dumps({"inert_log_files_checked": checked, "connection_attempt_markers": 0}))
""".strip()

_HEARTBEAT_SCRIPT = r"""
import json, pathlib, time
path = pathlib.Path("/heartbeat/bambu-publisher.heartbeat")
if not path.is_file() or path.is_symlink():
    raise SystemExit("heartbeat missing")
stamp = float(path.read_text(encoding="utf-8").strip())
age = time.time() - stamp
if age < 0 or age > 30:
    raise SystemExit("heartbeat stale")
print(json.dumps({"bambu_heartbeat_age_seconds": round(age, 3)}))
""".strip()


def _public_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _private_file_metadata(path: Path, *, maximum: int) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise OwnershipError(f"private controller artifact is unreadable: {path.name}") from exc
    if (
        path.is_symlink()
        or not path.is_file()
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or (metadata.st_mode & 0o777) != 0o600
        or metadata.st_size > maximum
    ):
        raise OwnershipError(f"private controller artifact is unsafe: {path.name}")
    return metadata


def _private_json(path: Path, value: dict[str, object]) -> None:
    _public_json(path, value)
    _private_file_metadata(path, maximum=4 * 1024 * 1024)


def _journal_digest(path: Path) -> str:
    _private_file_metadata(path, maximum=4 * 1024 * 1024)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OwnershipError("image ownership journal is unreadable") from exc
    if not isinstance(value, dict):
        raise OwnershipError("image ownership journal is invalid")
    immutable = {
        key: item
        for key, item in value.items()
        if key not in {"cleanup", "recovery_error", "cleanup_errors"}
    }
    payload = json.dumps(
        immutable, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _load_image_journal(path: Path) -> dict[str, object]:
    _private_file_metadata(path, maximum=4 * 1024 * 1024)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OwnershipError("image ownership journal is unreadable") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise OwnershipError("image ownership journal schema is invalid")
    status = value.get("capture_status")
    if status not in {"pending", "succeeded"}:
        raise OwnershipError("image ownership journal status is invalid")
    if value.get("iid_artifact") != "image.iid":
        raise OwnershipError("image ownership journal IID artifact is invalid")
    protected = value.get("protected_history")
    if not isinstance(protected, dict):
        raise OwnershipError("image ownership protected history is invalid")
    protected_ids = protected.get("ids")
    if (
        not isinstance(protected_ids, list)
        or not all(isinstance(item, str) and _IMAGE_ID.fullmatch(item) for item in protected_ids)
        or len(set(protected_ids)) != len(protected_ids)
        or protected.get("count") != len(protected_ids)
    ):
        raise OwnershipError("image ownership protected identities are invalid")
    digest = hashlib.sha256(
        "".join(f"{item}\n" for item in sorted(protected_ids)).encode("ascii")
    ).hexdigest()
    if protected.get("sha256") != digest:
        raise OwnershipError("image ownership protected digest is invalid")
    capacity = value.get("capacity")
    if not isinstance(capacity, dict) or set(capacity) != {
        "probe_image_id",
        "command_argv",
        "required_free_kib",
        "observed_free_kib",
        "passed",
        "observed_at",
    }:
        raise OwnershipError("image ownership capacity evidence is invalid")
    observed_free = capacity.get("observed_free_kib")
    if (
        capacity.get("probe_image_id") != PROBE_IMAGE_ID
        or capacity.get("command_argv") != list(VALIDATION_CAPACITY_COMMAND)
        or capacity.get("required_free_kib") != VALIDATION_DOCKER_MIN_FREE_KIB
        or not isinstance(observed_free, int)
        or isinstance(observed_free, bool)
        or observed_free < 0
        or not isinstance(capacity.get("passed"), bool)
        or capacity.get("passed") is not True
        or observed_free < VALIDATION_DOCKER_MIN_FREE_KIB
        or not isinstance(capacity.get("observed_at"), str)
    ):
        raise OwnershipError("image ownership capacity evidence is inconsistent")
    try:
        observed_at = datetime.fromisoformat(
            str(capacity["observed_at"]).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise OwnershipError("image ownership capacity timestamp is invalid") from exc
    if observed_at.tzinfo is None:
        raise OwnershipError("image ownership capacity timestamp is invalid")
    final_image_id = value.get("final_image_id")
    if final_image_id is not None and (
        not isinstance(final_image_id, str) or not _IMAGE_ID.fullmatch(final_image_id)
    ):
        raise OwnershipError("image ownership final identity is invalid")
    if status == "pending":
        token = value.get("owner_token")
        if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token):
            raise OwnershipError("pending image ownership token is invalid")
    else:
        if value.get("owner_token") is not None or not re.fullmatch(
            r"[0-9a-f]{64}", str(value.get("owner_token_sha256", ""))
        ):
            raise OwnershipError("succeeded image ownership token digest is invalid")
        for key in ("owned_root_ids", "owned_history_ids", "unowned_new_ids"):
            items = value.get(key)
            if not isinstance(items, list) or not all(
                isinstance(item, str) and _IMAGE_ID.fullmatch(item) for item in items
            ) or len(set(items)) != len(items):
                raise OwnershipError(f"image ownership {key} is invalid")
        cleanup = value.get("cleanup")
        if not isinstance(cleanup, dict) or not set(cleanup).issubset(
            {"tag", "history", "errors"}
        ):
            raise OwnershipError("image ownership cleanup progress is invalid")
        if cleanup.get("tag") not in {
            "pending",
            "removed",
            "already_absent",
            "unauthorized",
            "refused",
        }:
            raise OwnershipError("image ownership tag cleanup progress is invalid")
        history = cleanup.get("history")
        owned_history = value.get("owned_history_ids")
        assert isinstance(owned_history, list)
        if not isinstance(history, list) or len(history) > len(owned_history):
            raise OwnershipError("image ownership history cleanup progress is invalid")
        expected_record_fields = {
            "image_id",
            "status",
            "observed_image_id",
            "repo_tags",
            "repo_digests",
            "container_conflicts",
            "removed",
            "absent_after",
            "error",
        }
        for index, record in enumerate(history):
            if (
                not isinstance(record, dict)
                or set(record) != expected_record_fields
                or record.get("image_id") != owned_history[index]
                or record.get("status") not in {"REMOVED", "ALREADY_ABSENT", "FAIL"}
                or not isinstance(record.get("container_conflicts"), list)
                or not all(
                    isinstance(item, str)
                    for item in record.get("container_conflicts", [])
                )
                or not isinstance(record.get("removed"), bool)
                or not isinstance(record.get("absent_after"), bool)
            ):
                raise OwnershipError(
                    "image ownership history cleanup record is invalid"
                )
            if record["status"] == "REMOVED" and not (
                record["observed_image_id"] == record["image_id"]
                and record["repo_tags"] == []
                and record["repo_digests"] == []
                and record["container_conflicts"] == []
                and record["removed"] is True
                and record["absent_after"] is True
                and record["error"] is None
            ):
                raise OwnershipError("removed image history evidence is invalid")
            if record["status"] == "ALREADY_ABSENT" and not (
                record["observed_image_id"] is None
                and record["repo_tags"] is None
                and record["repo_digests"] is None
                and record["container_conflicts"] == []
                and record["removed"] is False
                and record["absent_after"] is True
                and record["error"] is None
            ):
                raise OwnershipError("absent image history evidence is invalid")
            if record["status"] == "FAIL" and not isinstance(record["error"], str):
                raise OwnershipError("failed image history evidence is invalid")
        cleanup_errors = cleanup.get("errors", [])
        if not isinstance(cleanup_errors, list) or not all(
            isinstance(item, str) for item in cleanup_errors
        ):
            raise OwnershipError("image ownership cleanup errors are invalid")
    return value


def _decode_json_output(output: str, label: str) -> dict[str, object]:
    try:
        value = json.loads(output.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise SandboxError(f"{label} returned invalid evidence") from exc
    if not isinstance(value, dict):
        raise SandboxError(f"{label} evidence must be an object")
    return value


def _normalized_license(content: bytes) -> bytes:
    try:
        text = content.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise SecretInputError("license must be UTF-8") from exc
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = None
    if isinstance(value, dict) and isinstance(value.get("payload"), str) and isinstance(value.get("signature"), str):
        text = value["payload"] + "." + value["signature"]
    if not text or len(text.split(".")) != 2:
        raise SecretInputError("license artifact has an invalid envelope")
    return text.encode("utf-8")


def _lease_deadline(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("lease expiry must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValidationError("lease expiry must include UTC timezone")
    return parsed.astimezone(timezone.utc)


def _license_deadline(value: str) -> datetime:
    try:
        if "T" not in value:
            return datetime.combine(date.fromisoformat(value), datetime_time.max, tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SandboxError("verified license returned an invalid expiry") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class SandboxRuntime:
    def __init__(self, state_root: Path = DEFAULT_STATE_ROOT, executor: Executor | None = None):
        self.state_root = validate_state_root(state_root)
        self.executor = executor or Executor()

    def _paths(self, sandbox_id: str) -> SandboxPaths:
        return SandboxPaths.create(self.state_root, sandbox_id)

    def _run(self, args: list[str], **kwargs):
        return self.executor.run(args, cwd=ROOT, **kwargs)

    def _docker_json(self, args: list[str], label: str) -> dict[str, object]:
        return _decode_json_output(self._run(args).stdout, label)

    def _try_run(self, args: list[str], **kwargs):
        return self.executor.run(args, cwd=ROOT, check=False, **kwargs)

    def _image_command(
        self, args: list[str], *, capture: bool = True, check: bool = True
    ):
        del capture
        return self.executor.run(args, cwd=ROOT, check=check, timeout=120)

    def _create_private_iid(self, paths: SandboxPaths) -> None:
        descriptor = os.open(
            paths.image_iid,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.close(descriptor)
        _private_file_metadata(paths.image_iid, maximum=80)

    def _read_private_iid(self, paths: SandboxPaths) -> str:
        _private_file_metadata(paths.image_iid, maximum=80)
        return read_iidfile(paths.image_iid)

    def _pending_image_journal(
        self, lifecycle: DisposableImageLifecycle, paths: SandboxPaths
    ) -> dict[str, object]:
        if lifecycle.protected is None or lifecycle.capacity is None:
            raise SandboxError("image lifecycle evidence is incomplete before build")
        journal: dict[str, object] = {
            "schema_version": 1,
            "capture_status": "pending",
            "candidate_tag": lifecycle.tag,
            "owner_token": lifecycle.owner_token,
            "owner_token_sha256": None,
            "iid_artifact": paths.image_iid.name,
            "final_image_id": None,
            "protected_history": {
                "ids": list(lifecycle.protected.protected_ids),
                "count": len(lifecycle.protected.protected_ids),
                "sha256": lifecycle.protected.protected_sha256,
            },
            "capacity": dict(lifecycle.capacity),
            "owned_root_ids": [],
            "owned_history_ids": [],
            "unowned_new_ids": [],
            "cleanup": {"tag": "pending", "history": []},
        }
        _private_json(paths.image_journal, journal)
        return journal

    def _succeeded_image_journal(
        self,
        lifecycle: DisposableImageLifecycle,
        paths: SandboxPaths,
        journal: dict[str, object],
    ) -> dict[str, object]:
        if not lifecycle.ownership_capture_succeeded:
            raise SandboxError("image ownership capture did not succeed")
        value = dict(journal)
        value.update(
            {
                "capture_status": "succeeded",
                "owner_token": None,
                "owner_token_sha256": hashlib.sha256(
                    lifecycle.owner_token.encode("ascii")
                ).hexdigest(),
                "final_image_id": lifecycle.final_image_id,
                "owned_root_ids": list(lifecycle.owned_root_ids),
                "owned_history_ids": list(lifecycle.owned_history_ids),
                "unowned_new_ids": list(lifecycle.unowned_new_ids),
                "cleanup": {"tag": "pending", "history": []},
            }
        )
        _private_json(paths.image_journal, value)
        return value

    def _lifecycle_from_journal(
        self, journal: dict[str, object]
    ) -> DisposableImageLifecycle:
        if journal.get("capture_status") != "succeeded":
            raise OwnershipError("fixed image ownership capture is not available")
        protected = journal["protected_history"]
        assert isinstance(protected, dict)
        protected_ids = tuple(str(item) for item in protected["ids"])
        lifecycle = DisposableImageLifecycle(
            str(journal["candidate_tag"]), command=self._image_command
        )
        lifecycle.lock = acquire_image_lifecycle_lock()
        lifecycle.capacity = dict(journal["capacity"])  # type: ignore[arg-type]
        lifecycle.protected = ProtectedImageHistory(
            visible_ids=(),
            protected_ids=protected_ids,
            protected_sha256=str(protected["sha256"]),
        )
        lifecycle.build_attempted = True
        final_image_id = journal.get("final_image_id")
        lifecycle.final_image_id = str(final_image_id) if final_image_id else None
        lifecycle.ownership_capture_attempted = True
        lifecycle.ownership_capture_succeeded = True
        lifecycle.owned_root_ids = tuple(str(item) for item in journal["owned_root_ids"])
        lifecycle.owned_history_ids = tuple(
            str(item) for item in journal["owned_history_ids"]
        )
        lifecycle.unowned_new_ids = tuple(
            str(item) for item in journal["unowned_new_ids"]
        )
        return lifecycle

    def _cleanup_fixed_image_journal(
        self, paths: SandboxPaths, journal: dict[str, object]
    ) -> tuple[dict[str, object], list[str]]:
        lifecycle = self._lifecycle_from_journal(journal)
        errors: list[str] = []
        cleanup = dict(journal.get("cleanup", {}))
        history_progress = list(cleanup.get("history", []))
        history_by_id = {
            str(item["image_id"]): item
            for item in history_progress
            if isinstance(item, dict) and isinstance(item.get("image_id"), str)
        }
        completed_history = {
            str(item.get("image_id"))
            for item in history_progress
            if isinstance(item, dict) and item.get("status") in {"REMOVED", "ALREADY_ABSENT"}
        }
        try:
            final_image_id = lifecycle.final_image_id
            if cleanup.get("tag") not in {"removed", "already_absent"}:
                if final_image_id is None:
                    cleanup["tag"] = "unauthorized"
                else:
                    current = inspect_image_id(lifecycle.tag, command=self._image_command)
                    if current is None:
                        cleanup["tag"] = "already_absent"
                    elif current != final_image_id:
                        errors.append("image tag identity changed; deletion refused")
                        cleanup["tag"] = "refused"
                    else:
                        remove_owned_image(
                            lifecycle.tag,
                            final_image_id,
                            command=self._image_command,
                        )
                        cleanup["tag"] = "removed"
                journal["cleanup"] = cleanup
                _private_json(paths.image_journal, journal)
            if not errors:
                for image_id in lifecycle.owned_history_ids:
                    if image_id in completed_history:
                        continue
                    records, item_errors = cleanup_owned_image_history(
                        (image_id,), command=self._image_command
                    )
                    if len(records) != 1 or records[0].get("image_id") != image_id:
                        raise OwnershipError(
                            "image history cleanup returned malformed progress"
                        )
                    history_by_id[image_id] = records[0]
                    history_progress = [
                        history_by_id[owned_id]
                        for owned_id in lifecycle.owned_history_ids
                        if owned_id in history_by_id
                    ]
                    cleanup["history"] = history_progress
                    journal["cleanup"] = cleanup
                    _private_json(paths.image_journal, journal)
                    errors.extend(item_errors)
                    if item_errors:
                        break
            if not errors:
                errors.extend(
                    verify_owned_image_history_absent(
                        lifecycle.owned_history_ids,
                        command=self._image_command,
                    )
                )
            cleanup["errors"] = list(errors)
            journal["cleanup"] = cleanup
            _private_json(paths.image_journal, journal)
        finally:
            errors.extend(release_image_lifecycle_lock(lifecycle.lock))
            lifecycle.lock = None
        return journal, errors

    def _record_lifecycle_cleanup(
        self,
        paths: SandboxPaths,
        journal: dict[str, object],
        lifecycle: DisposableImageLifecycle,
        errors: list[str],
    ) -> dict[str, object]:
        cleanup = dict(journal.get("cleanup", {}))
        if lifecycle.final_image_id is None:
            cleanup["tag"] = "unauthorized"
        elif not any(message.startswith("image tag:") for message in errors):
            cleanup["tag"] = "removed"
        else:
            cleanup["tag"] = "refused"
        cleanup["history"] = list(lifecycle.history_cleanup or [])
        cleanup["errors"] = list(errors)
        journal["cleanup"] = cleanup
        _private_json(paths.image_journal, journal)
        return journal

    def _cleanup_failed_prepare_resources(
        self, state: LifecycleState, resources: ResourceSet
    ) -> list[str]:
        errors: list[str] = []
        try:
            container_ids = self._try_run(
                [
                    "docker",
                    "ps",
                    "-aq",
                    "--filter",
                    f"label={OWNER_LABEL}={state.sandbox_id}",
                ],
                timeout=30,
            ).stdout.splitlines()
            for container_id in filter(None, container_ids):
                name = self._run(
                    ["docker", "inspect", "--format", "{{.Name}}", container_id]
                ).stdout.strip().lstrip("/")
                if not name.startswith(resources.project + "-"):
                    raise OwnershipError(
                        "failed prepare has a foreign labelled container name"
                    )
                self._assert_owned("container", container_id, state.sandbox_id)
                self._run(["docker", "rm", "-f", container_id], timeout=60)
            for kind, names in (
                (
                    "volume",
                    (
                        resources.data_volume,
                        resources.secret_volume,
                        resources.heartbeat_volume,
                    ),
                ),
                (
                    "network",
                    (resources.internal_network, resources.edge_network),
                ),
            ):
                for name in names:
                    if self._assert_owned(
                        kind, name, state.sandbox_id, expected_generation=0
                    ):
                        self._run(["docker", kind, "rm", name], timeout=60)
        except Exception as exc:
            errors.append(str(exc))
        return errors

    def _git(self, *args: str) -> str:
        return self._run(["git", *args]).stdout.strip()

    def _resource_create(self, args: list[str], sandbox_id: str) -> None:
        self._run([
            *args[:-1],
            "--label", f"{OWNER_LABEL}={sandbox_id}",
            "--label", f"{SCHEMA_LABEL}=1",
            "--label", f"{GENERATION_LABEL}=0",
            "--label", f"{PROJECT_LABEL}={compose_project(sandbox_id)}",
            args[-1],
        ])

    def _write_generated_secrets(self, paths: SandboxPaths, resources: ResourceSet, image_id: str) -> None:
        values = {
            "api-key": random_secrets.token_urlsafe(36),
            "jwt-secret": random_secrets.token_urlsafe(48),
            "encryption-key": Fernet.generate_key().decode("ascii"),
            "admin-password": "Edu-Admin-Aa1!-" + random_secrets.token_urlsafe(18),
            "teacher-password": "Edu-Teacher-Aa1!-" + random_secrets.token_urlsafe(18),
            "student-password": "Edu-Student-Aa1!-" + random_secrets.token_urlsafe(18),
        }
        for name, value in values.items():
            write_secret(paths.secrets / name, value.encode("utf-8"), paths.directory)
        payload = json.dumps(values, separators=(",", ":")).encode("utf-8")
        self._run(
            ["docker", "run", "--rm", "--name", _helper_container_name(resources.project, "secret-seed"), "-i", "--network", "none",
             "--label", f"{OWNER_LABEL}={paths.directory.name}", "--label", f"{SCHEMA_LABEL}=1",
             "--label", f"{GENERATION_LABEL}=0",
             "--label", f"{PROJECT_LABEL}={compose_project(paths.directory.name)}",
             "-v", f"{resources.secret_volume}:/run/odin-secrets", "--entrypoint", "python3", image_id, "-c", _SECRET_VOLUME_SCRIPT],
            input_bytes=payload,
        )

    def _wait_ready(self, port: int, timeout: int = 240) -> None:
        deadline = time.monotonic() + timeout
        last = "not attempted"
        while time.monotonic() < deadline:
            connection = HTTPConnection("127.0.0.1", port, timeout=3)
            try:
                connection.request("GET", "/health/ready")
                response = connection.getresponse()
                payload = json.loads(response.read())
                if response.status == 200 and payload.get("ready") is True:
                    return
                last = f"HTTP {response.status}"
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                last = type(exc).__name__
            finally:
                connection.close()
            time.sleep(1)
        raise SandboxError(f"sandbox readiness timed out: {last}")

    def prepare(
        self, sandbox_id: str, *, allow_dirty: bool = False, recover: bool = False
    ) -> LifecycleState:
        sandbox_id = validate_sandbox_id(sandbox_id)
        paths = self._paths(sandbox_id)
        resources = ResourceSet.from_id(sandbox_id)
        tombstone_base = paths.root / ".tombstones" / sandbox_id
        if tombstone_base.with_suffix(".json").exists() or tombstone_base.with_suffix(".html").exists():
            raise StateError("sandbox ID is terminally PURGED and cannot be reused")
        if paths.state.exists() or paths.directory.exists() or paths.directory.is_symlink():
            if not recover:
                raise StateError("sandbox state already exists; use prepare --recover only for interrupted preparation")
            if paths.state.exists():
                self._recover_interrupted_prepare(paths, resources)
            else:
                remnants, _ = self._assert_unpublished_prepare(paths, resources)
                self._remove_unpublished_prepare(paths, remnants)
        dirty = bool(self._git("status", "--porcelain"))
        if dirty and not allow_dirty:
            raise StateError("prepare requires a clean tree; use --allow-dirty to record local evidence")
        paths.directory.mkdir(mode=0o700, parents=True)
        state = LifecycleState(
            sandbox_id=sandbox_id,
            phase=Phase.PREPARING,
            source_commit=self._git("rev-parse", "HEAD"),
            source_dirty=dirty,
            candidate_tag=f"odin-edu-candidate:{sandbox_id}",
            compose_project=resources.project,
            resources=resources.state_value(),
        )
        save_state(paths.state, state, paths.directory)
        image_lifecycle = DisposableImageLifecycle(
            state.candidate_tag, command=self._image_command
        )
        image_journal: dict[str, object] | None = None
        lease_committed = False
        try:
            try:
                image_lifecycle.begin()
            except Exception:
                if image_lifecycle.capacity is not None:
                    state.verification["image_capacity"] = image_lifecycle.capacity
                    save_state(paths.state, state, paths.directory)
                raise
            self._create_private_iid(paths)
            image_journal = self._pending_image_journal(
                image_lifecycle, paths
            )
            image_lifecycle.mark_build_attempted()
            try:
                self._run(
                    [
                        "docker",
                        "build",
                        "--pull",
                        "--iidfile",
                        str(paths.image_iid),
                        *image_lifecycle.docker_build_owner_args(),
                        "-t",
                        state.candidate_tag,
                        ".",
                    ],
                    timeout=1800,
                )
                image_id = self._read_private_iid(paths)
                if verify_built_image(
                    state.candidate_tag,
                    paths.image_iid,
                    command=self._image_command,
                ) != image_id:
                    raise SandboxError("candidate image IID verification changed")
                image_lifecycle.establish_final_image(image_id)
                image_journal["final_image_id"] = image_id
                _private_json(paths.image_journal, image_journal)
                image_lifecycle.freeze_ownership()
                image_journal = self._succeeded_image_journal(
                    image_lifecycle, paths, image_journal
                )
                paths.image_iid.unlink()
            except BaseException:
                if not image_lifecycle.ownership_capture_attempted:
                    try:
                        image_lifecycle.freeze_ownership()
                        image_journal = self._succeeded_image_journal(
                            image_lifecycle, paths, image_journal
                        )
                        paths.image_iid.unlink(missing_ok=True)
                    except Exception:
                        pass
                raise
            state.candidate_image_id = image_id
            self._run(["docker", "pull", BROKER_IMAGE], timeout=600)
            broker_image_id = self._run(
                ["docker", "image", "inspect", "--format", "{{.Id}}", BROKER_IMAGE]
            ).stdout.strip()
            if not _IMAGE_ID.fullmatch(broker_image_id):
                raise SandboxError("broker did not expose an immutable content ID")
            state.broker_image_id = broker_image_id
            state.broker_digest = BROKER_IMAGE.split("@sha256:", 1)[-1]
            self._resource_create(["docker", "network", "create", "--internal", resources.internal_network], sandbox_id)
            self._resource_create(["docker", "network", "create", resources.edge_network], sandbox_id)
            for volume in (resources.data_volume, resources.secret_volume, resources.heartbeat_volume):
                self._resource_create(["docker", "volume", "create", volume], sandbox_id)
            self._write_generated_secrets(paths, resources, image_id)
            self._run([
                "docker", "run", "-d", "--name", resources.prepare_container,
                "--hostname", f"{resources.project}-app", "--network", resources.internal_network,
                "--label", f"{OWNER_LABEL}={sandbox_id}", "--label", f"{SCHEMA_LABEL}=1",
                "--label", f"{GENERATION_LABEL}=0",
                "--label", f"{PROJECT_LABEL}={state.compose_project}",
                "-v", f"{resources.data_volume}:/data",
                "-v", f"{resources.secret_volume}:/run/odin-secrets:ro",
                "-e", "ENCRYPTION_KEY_FILE=/run/odin-secrets/encryption-key",
                "-e", "JWT_SECRET_KEY_FILE=/run/odin-secrets/jwt-secret",
                "-e", "API_KEY_FILE=/run/odin-secrets/api-key",
                "-e", "DATABASE_URL=sqlite:////data/odin.db",
                "-e", "CORS_ORIGINS=http://127.0.0.1", "-e", "TRUSTED_HOSTS=127.0.0.1,localhost",
                "-e", "COOKIE_SECURE=false", image_id,
            ], timeout=60)
            self._run([
                "docker", "run", "-d", "--name", resources.prepare_proxy,
                "--network", resources.edge_network,
                "--label", f"{OWNER_LABEL}={sandbox_id}", "--label", f"{SCHEMA_LABEL}=1",
                "--label", f"{GENERATION_LABEL}=0",
                "--label", f"{PROJECT_LABEL}={state.compose_project}",
                "--read-only", "--user", "10001:10001", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges:true",
                "-e", f"ODIN_EDU_PROXY_TARGET={resources.prepare_container}",
                "-p", "127.0.0.1::8080", "--entrypoint", "python3", image_id,
                "/app/ops/edu_sandbox/tcp_proxy.py",
            ], timeout=60)
            self._run([
                "docker", "network", "connect", resources.internal_network, resources.prepare_proxy
            ], timeout=30)
            port_text = self._run(["docker", "port", resources.prepare_proxy, "8080/tcp"]).stdout.strip()
            match = _PORT.search(port_text)
            if not match:
                raise SandboxError("Docker did not allocate a loopback port")
            state.loopback_port = int(match.group(1))
            state.verification = {
                "prepare_network_isolation": self._assert_network_isolation(
                    state, preparing=True
                ),
                "image_capacity": image_lifecycle.capacity,
                "image_ownership_journal_sha256": _journal_digest(
                    paths.image_journal
                ),
            }
            state.image_ownership_journal_sha256 = str(
                state.verification["image_ownership_journal_sha256"]
            )
            self._wait_ready(state.loopback_port)
            running_image = self._run(["docker", "inspect", "--format", "{{.Image}}", resources.prepare_container]).stdout.strip()
            if running_image != image_id:
                raise SandboxError("running application image does not match the built candidate")
            proxy_image = self._run(["docker", "inspect", "--format", "{{.Image}}", resources.prepare_proxy]).stdout.strip()
            if proxy_image != image_id:
                raise SandboxError("prepare proxy image does not match the built candidate")
            identity = self._docker_json(
                ["docker", "exec", "-w", "/app/backend", resources.prepare_container, "python3", "-c", _IDENTITY_SCRIPT],
                "candidate identity",
            )
            state.installation_id = str(identity.get("installation_id", ""))
            state.device_public_key_sha256 = str(identity.get("device_public_key_sha256", ""))
            odin_version = str(identity.get("odin_version", ""))
            if not state.installation_id or not re.fullmatch(r"[0-9a-f-]{36}", state.installation_id):
                raise SandboxError("candidate installation ID is invalid")
            if not re.fullmatch(r"[0-9a-f]{64}", state.device_public_key_sha256):
                raise SandboxError("candidate device-key fingerprint is invalid")
            if not odin_version or len(odin_version) > 64:
                raise SandboxError("candidate ODIN version is invalid")
            self._run(["docker", "rm", "-f", resources.prepare_proxy, resources.prepare_container])
            receipt = {
                "schema_version": 1,
                "sandbox_id": sandbox_id,
                "installation_id": state.installation_id,
                "odin_version": odin_version,
                "device_public_key_sha256": state.device_public_key_sha256,
                "candidate_image_id": image_id,
                "source_commit": state.source_commit,
                "generated_at": utc_now(),
            }
            _public_json(paths.public / "activation-receipt.json", receipt)
            state.transition(Phase.PREPARED, detail="exact candidate stopped with installation identity intact")
            save_state(paths.state, state, paths.directory)
            lease_committed = True
            lease_release_errors = image_lifecycle.defer_cleanup_for_lease()
            if lease_release_errors:
                raise SandboxError(
                    "image lifecycle lease lock release failed: "
                    + "; ".join(lease_release_errors)
                )
            return state
        except BaseException as exc:
            resource_cleanup_errors: list[str] = []
            image_cleanup_errors: list[str] = []
            if lease_committed:
                image_cleanup_errors.extend(
                    release_image_lifecycle_lock(image_lifecycle.lock)
                )
                image_lifecycle.lock = None
            else:
                resource_cleanup_errors = self._cleanup_failed_prepare_resources(
                    state, resources
                )
            if not lease_committed and image_lifecycle.lock is not None:
                if (
                    image_journal is not None
                    and image_journal.get("capture_status") == "succeeded"
                ):
                    image_cleanup_errors = image_lifecycle.finalize()
                    image_journal = self._record_lifecycle_cleanup(
                        paths,
                        image_journal,
                        image_lifecycle,
                        image_cleanup_errors,
                    )
                else:
                    image_cleanup_errors = [
                        "image ownership capture was not durably journaled; "
                        "no image deletion issued"
                    ]
                    image_cleanup_errors.extend(
                        release_image_lifecycle_lock(image_lifecycle.lock)
                    )
                    image_lifecycle.lock = None
            if state.phase == Phase.PREPARING:
                detail = f"prepare failed: {type(exc).__name__}"
                cleanup_errors = resource_cleanup_errors + image_cleanup_errors
                if cleanup_errors:
                    detail += "; cleanup: " + "; ".join(cleanup_errors)
                state.transition(Phase.DEGRADED, detail=detail)
                save_state(paths.state, state, paths.directory)
            raise

    def _recover_interrupted_request(
        self, paths: SandboxPaths, state: LifecycleState
    ) -> LifecycleState:
        if state.last_transition.get("from") != Phase.PREPARED.value:
            raise StateError("REQUESTING_LICENSE recovery origin is invalid")
        self._assert_prepared_identity(state)
        (paths.secrets / ACTIVATION_REQUEST_FILENAME).unlink(missing_ok=True)
        (paths.public / "activation-request-receipt.json").unlink(missing_ok=True)
        state.phase = Phase.PREPARED
        state.last_transition = {
            "from": Phase.REQUESTING_LICENSE.value,
            "to": Phase.PREPARED.value,
            "at": utc_now(),
            "detail": "interrupted request recovered; partial handoff removed",
        }
        save_state(paths.state, state, paths.directory)
        return state

    def request_license(self, sandbox_id: str, stream, *, is_tty: bool) -> dict[str, object]:
        paths = self._paths(sandbox_id)
        state = load_state(paths.state)
        if state.phase == Phase.REQUESTING_LICENSE:
            state = self._recover_interrupted_request(paths, state)
        if state.phase != Phase.PREPARED:
            raise StateError("request-license requires PREPARED phase")
        self._assert_prepared_identity(state)
        body = read_json_object(stream, limit=REQUEST_LICENSE_LIMIT, is_tty=is_tty)
        if set(body) != {"key", "nonce"} or not all(isinstance(body[name], str) and body[name] for name in body):
            raise SecretInputError("request-license requires exactly non-empty key and nonce strings")
        state.transition(Phase.REQUESTING_LICENSE, detail="creating signed activation request")
        save_state(paths.state, state, paths.directory)
        try:
            resources = ResourceSet.from_id(sandbox_id)
            result = self._run(
                ["docker", "run", "--rm", "--name", _helper_container_name(state.compose_project, "license-request"), "-i", "--network", "none",
                 "--label", f"{OWNER_LABEL}={sandbox_id}", "--label", f"{SCHEMA_LABEL}=1",
                 "--label", f"{GENERATION_LABEL}={state.reset_generation}",
                 "--label", f"{PROJECT_LABEL}={state.compose_project}",
                 "-e", f"ODIN_EDU_SANDBOX_ID={sandbox_id}", "-v", f"{resources.data_volume}:/data:ro", "-w", "/app/backend",
                 "--entrypoint", "python3", state.candidate_image_id, "-c", _REQUEST_LICENSE_SCRIPT],
                input_bytes=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            )
            secret_payload = result.stdout.strip().encode("utf-8")
            parsed = _decode_json_output(result.stdout, "activation request")
            activation = parsed.get("activation_request")
            if not isinstance(activation, dict) or activation.get("installation_id") != state.installation_id:
                raise SandboxError("activation request identity does not match prepared sandbox")
            digest = write_secret(paths.secrets / ACTIVATION_REQUEST_FILENAME, secret_payload, paths.directory)
            receipt = {
                "schema_version": 1,
                "sandbox_id": sandbox_id,
                "installation_id": state.installation_id,
                "device_public_key_sha256": state.device_public_key_sha256,
                "activation_request_sha256": digest,
                "generated_at": utc_now(),
            }
            _public_json(paths.public / "activation-request-receipt.json", receipt)
            state.transition(Phase.PREPARED, detail="signed activation request stored for out-of-band delivery")
            save_state(paths.state, state, paths.directory)
            return receipt
        except BaseException:
            (paths.secrets / ACTIVATION_REQUEST_FILENAME).unlink(missing_ok=True)
            state.phase = Phase.PREPARED
            state.last_transition = {"from": Phase.REQUESTING_LICENSE.value, "to": Phase.PREPARED.value, "at": utc_now(), "detail": "request failed; secret input removed"}
            save_state(paths.state, state, paths.directory)
            raise

    def _validate_license(self, state: LifecycleState, resources: ResourceSet, content: bytes) -> dict[str, object]:
        result = self._run(
            ["docker", "run", "--rm", "--name", _helper_container_name(state.compose_project, "license-validate"), "-i", "--network", "none",
             "--label", f"{OWNER_LABEL}={state.sandbox_id}", "--label", f"{SCHEMA_LABEL}=1",
             "--label", f"{GENERATION_LABEL}={state.reset_generation}",
             "--label", f"{PROJECT_LABEL}={state.compose_project}",
             "-e", "ODIN_REQUIRE_LICENSE_BINDING=1",
             "-e", f"ODIN_EXPECTED_INSTALLATION_ID={state.installation_id}",
             "-w", "/app/backend",
             "--entrypoint", "python3", state.candidate_image_id, "-c", _VALIDATE_LICENSE_SCRIPT],
            input_bytes=content,
        )
        return _decode_json_output(result.stdout, "license validation")

    def _compose_env(self, state: LifecycleState) -> dict[str, str]:
        resources = ResourceSet.from_id(state.sandbox_id)
        return {
            "ODIN_EDU_SANDBOX_ID": state.sandbox_id,
            "ODIN_EDU_CANDIDATE_IMAGE_ID": state.candidate_image_id,
            "ODIN_EDU_INTERNAL_NETWORK": resources.internal_network,
            "ODIN_EDU_EDGE_NETWORK": resources.edge_network,
            "ODIN_EDU_DATA_VOLUME": resources.data_volume,
            "ODIN_EDU_SECRET_VOLUME": resources.secret_volume,
            "ODIN_EDU_HEARTBEAT_VOLUME": resources.heartbeat_volume,
            "ODIN_EDU_BROKER_IMAGE": BROKER_IMAGE,
            "ODIN_EDU_GENERATION": str(
                state.reset_generation + (1 if state.phase == Phase.RESETTING else 0)
            ),
        }

    def _compose(self, state: LifecycleState, *args: str, timeout: int = 300):
        return self._run(
            ["docker", "compose", "-p", state.compose_project, "-f", str(COMPOSE_FILE), *args],
            env=self._compose_env(state),
            timeout=timeout,
        )

    @staticmethod
    def _network_member_names(payload: dict[str, object]) -> list[str]:
        containers = payload.get("Containers")
        if not isinstance(containers, dict):
            raise SandboxError("sandbox network membership evidence is unreadable")
        names: set[str] = set()
        for attachment in containers.values():
            name = attachment.get("Name") if isinstance(attachment, dict) else None
            if not isinstance(name, str) or not name:
                raise SandboxError("sandbox network membership evidence is unreadable")
            names.add(name)
        return sorted(names)

    def _assert_network_isolation(
        self, state: LifecycleState, *, preparing: bool = False
    ) -> dict[str, object]:
        resources = ResourceSet.from_id(state.sandbox_id)
        network_payloads: dict[str, dict[str, object]] = {}
        for label, name in (
            ("sandbox", resources.internal_network),
            ("edge", resources.edge_network),
        ):
            try:
                payload = json.loads(
                    self._run(["docker", "network", "inspect", name], timeout=30).stdout
                )
                network_payloads[label] = payload[0]
            except (IndexError, TypeError, json.JSONDecodeError) as exc:
                raise SandboxError("sandbox network isolation evidence is unreadable") from exc
        if network_payloads["sandbox"].get("Internal") is not True:
            raise SandboxError("sandbox application network is not internal-only")
        if network_payloads["edge"].get("Internal") is True:
            raise SandboxError("loopback proxy edge cannot publish from an internal network")

        if preparing:
            expected = {
                resources.prepare_container: {resources.internal_network},
                resources.prepare_proxy: {resources.internal_network, resources.edge_network},
            }
            proxy_name = resources.prepare_proxy
            app_name = resources.prepare_container
        else:
            expected = {
                f"{state.compose_project}-odin-1": {resources.internal_network},
                f"{state.compose_project}-publisher-1": {resources.internal_network},
                f"{state.compose_project}-mosquitto-1": {resources.internal_network},
                f"{state.compose_project}-proxy-1": {
                    resources.internal_network,
                    resources.edge_network,
                },
            }
            proxy_name = f"{state.compose_project}-proxy-1"
            app_name = f"{state.compose_project}-odin-1"

        network_members: dict[str, list[str]] = {}
        for label, payload in network_payloads.items():
            network_members[label] = self._network_member_names(payload)

        expected_internal_members = set(expected)
        if set(network_members["sandbox"]) != expected_internal_members:
            raise SandboxError("sandbox internal network has unexpected members")
        if set(network_members["edge"]) != {proxy_name}:
            raise SandboxError("sandbox edge network must contain only the loopback proxy")

        attachments: dict[str, list[str]] = {}
        for name, required in expected.items():
            result = self._run(
                ["docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", name],
                timeout=30,
            )
            try:
                networks = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                raise SandboxError("container network evidence is unreadable") from exc
            actual = set(networks) if isinstance(networks, dict) else set()
            if actual != required:
                raise SandboxError(f"container network isolation mismatch: {name}")
            attachments[name] = sorted(actual)
        for name in (app_name, proxy_name):
            result = self._run(
                ["docker", "inspect", "--format", "{{json .HostConfig.PortBindings}}", name],
                timeout=30,
            )
            try:
                bindings = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                raise SandboxError("container port-binding evidence is unreadable") from exc
            if name == app_name and bindings:
                raise SandboxError("ODIN application container must not publish host ports")
            if name == proxy_name:
                values = bindings.get("8080/tcp") if isinstance(bindings, dict) else None
                if (
                    not isinstance(values, list)
                    or len(values) != 1
                    or values[0].get("HostIp") != "127.0.0.1"
                ):
                    raise SandboxError("proxy must have exactly one loopback-only binding")
        return {
            "sandbox_network_internal": True,
            "application_has_no_edge_route": True,
            "proxy_is_only_edge_member": True,
            "proxy_loopback_only": True,
            "network_members": network_members,
            "attachments": attachments,
        }

    def _put_volume_license(self, state: LifecycleState, resources: ResourceSet, content: bytes) -> None:
        self._run(
            ["docker", "run", "--rm", "--name", _helper_container_name(state.compose_project, "license-put"), "-i", "--network", "none",
             "--label", f"{OWNER_LABEL}={state.sandbox_id}", "--label", f"{SCHEMA_LABEL}=1",
             "--label", f"{GENERATION_LABEL}={state.reset_generation}",
             "--label", f"{PROJECT_LABEL}={state.compose_project}",
             "-v", f"{resources.secret_volume}:/run/odin-secrets", "--entrypoint", "python3",
             state.candidate_image_id, "-c", _PUT_LICENSE_SCRIPT],
            input_bytes=content,
        )

    def _remove_volume_license(self, state: LifecycleState, resources: ResourceSet) -> None:
        evidence = self._docker_json(
            ["docker", "run", "--rm", "--name", _helper_container_name(state.compose_project, "license-delete"), "--network", "none",
             "--label", f"{OWNER_LABEL}={state.sandbox_id}", "--label", f"{SCHEMA_LABEL}=1",
             "--label", f"{GENERATION_LABEL}={state.reset_generation}",
             "--label", f"{PROJECT_LABEL}={state.compose_project}",
             "-v", f"{resources.secret_volume}:/run/odin-secrets", "--entrypoint", "python3",
             state.candidate_image_id, "-c", _DELETE_LICENSE_SCRIPT],
            "license removal",
        )
        if evidence.get("license_absent") is not True:
            raise SandboxError("staged volume license was not removed")

    def _volume_license_digest(self, state: LifecycleState, resources: ResourceSet) -> str:
        evidence = self._docker_json(
            ["docker", "run", "--rm", "--name", _helper_container_name(state.compose_project, "license-digest"), "--network", "none",
             "--label", f"{OWNER_LABEL}={state.sandbox_id}", "--label", f"{SCHEMA_LABEL}=1",
             "--label", f"{GENERATION_LABEL}={state.reset_generation}",
             "--label", f"{PROJECT_LABEL}={state.compose_project}",
             "-v", f"{resources.secret_volume}:/run/odin-secrets:ro", "--entrypoint", "python3",
             state.candidate_image_id, "-c", _LICENSE_DIGEST_SCRIPT],
            "mounted license digest",
        )
        digest = str(evidence.get("license_sha256", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SandboxError("mounted license digest is invalid")
        return digest

    @staticmethod
    def _controller_license_bytes(paths: SandboxPaths, expected_digest: str) -> bytes:
        path = paths.secrets / LICENSE_FILENAME
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise SandboxError("controller license file is missing or has unsafe permissions")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected_digest:
            raise SandboxError("controller license digest does not match state")
        return content

    @staticmethod
    def _activation_request_digest(paths: SandboxPaths, state: LifecycleState) -> str:
        secret = paths.secrets / ACTIVATION_REQUEST_FILENAME
        receipt = paths.public / "activation-request-receipt.json"
        if secret.is_symlink() or not secret.is_file() or secret.stat().st_mode & 0o077:
            raise StateError("activate requires a safe proof-of-possession request")
        if receipt.is_symlink() or not receipt.is_file() or receipt.stat().st_mode & 0o077:
            raise StateError("activate requires a public activation-request receipt")
        digest = hashlib.sha256(secret.read_bytes()).hexdigest()
        try:
            value = json.loads(receipt.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StateError("activation-request receipt is unreadable") from exc
        if (
            not isinstance(value, dict)
            or value.get("sandbox_id") != state.sandbox_id
            or value.get("installation_id") != state.installation_id
            or value.get("device_public_key_sha256") != state.device_public_key_sha256
            or value.get("activation_request_sha256") != digest
        ):
            raise StateError("activation-request receipt does not match the prepared sandbox")
        return digest

    def _data_identity(self, state: LifecycleState, resources: ResourceSet) -> dict[str, object]:
        return self._docker_json(
            ["docker", "run", "--rm", "--name", _helper_container_name(state.compose_project, "identity-read"), "--network", "none",
             "--label", f"{OWNER_LABEL}={state.sandbox_id}", "--label", f"{SCHEMA_LABEL}=1",
             "--label", f"{GENERATION_LABEL}={state.reset_generation}",
             "--label", f"{PROJECT_LABEL}={state.compose_project}",
             "-v", f"{resources.data_volume}:/data:ro", "-w", "/app/backend",
             "--entrypoint", "python3", state.candidate_image_id, "-c", _READ_IDENTITY_SCRIPT],
            "persisted sandbox identity",
        )

    def _assert_prepared_identity(self, state: LifecycleState) -> ResourceSet:
        resources = self._assert_state_resource_ownership(state)
        identity = self._data_identity(state, resources)
        if (
            identity.get("installation_id") != state.installation_id
            or identity.get("device_public_key_sha256") != state.device_public_key_sha256
        ):
            raise SandboxError("persisted installation/device identity does not match state")
        return resources

    def _assert_prepared_current_evidence(
        self, state: LifecycleState
    ) -> dict[str, object]:
        """Prove a prepared sandbox is currently intact and fully quiesced."""
        if state.phase != Phase.PREPARED:
            raise StateError("prepared evidence requires PREPARED phase")
        resources = self._assert_prepared_identity(state)
        self._assert_named_containers_owned(
            state,
            allow_absent=True,
            include_prepare=True,
        )
        labelled = self._try_run(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                f"label={OWNER_LABEL}={state.sandbox_id}",
            ],
            timeout=30,
        )
        if labelled.returncode != 0:
            raise SandboxError("cannot verify prepared container absence")
        if any(line.strip() for line in labelled.stdout.splitlines()):
            raise SandboxError("prepared sandbox still has an owned container")

        network_members: dict[str, list[str]] = {}
        for label, name, expected_internal in (
            ("sandbox", resources.internal_network, True),
            ("edge", resources.edge_network, False),
        ):
            try:
                payload = json.loads(
                    self._run(["docker", "network", "inspect", name], timeout=30).stdout
                )[0]
            except (IndexError, TypeError, json.JSONDecodeError) as exc:
                raise SandboxError("prepared network evidence is unreadable") from exc
            if payload.get("Internal") is not expected_internal:
                raise SandboxError("prepared network isolation no longer matches policy")
            members = self._network_member_names(payload)
            if members:
                raise SandboxError("prepared network has an unexpected live member")
            network_members[label] = members
        return {
            "identity_matches": True,
            "owned_resources_match": True,
            "owned_containers_absent": True,
            "network_members": network_members,
            "networks_quiesced": True,
        }

    def _observe_bound_license(
        self,
        state: LifecycleState,
        resources: ResourceSet,
        *,
        include_application: bool,
    ) -> dict[str, object]:
        """Verify controller, mounted, cryptographic, and optional app license facts."""
        paths = self._paths(state.sandbox_id)
        content = self._controller_license_bytes(paths, state.license_sha256)
        mounted_digest = self._volume_license_digest(state, resources)
        evidence = self._validate_license(state, resources, content)
        digest = hashlib.sha256(content).hexdigest()
        if mounted_digest != digest or evidence.get("license_sha256") != digest:
            raise SandboxError("observed license copies do not have one verified digest")
        expires_at = str(evidence.get("expires_at", ""))
        deadline = _license_deadline(expires_at)
        expired = deadline < datetime.now(timezone.utc)
        if (
            evidence.get("tier") != "education"
            or evidence.get("binding_present") is not True
            or evidence.get("binding_matches_current") is not True
            or evidence.get("valid") is not (not expired)
            or expires_at != state.license_expires_at
        ):
            raise SandboxError("observed signed license does not match controller state")
        result: dict[str, object] = {
            "license_sha256": digest,
            "tier": "education",
            "expires_at": expires_at,
            "expired": expired,
            "binding_present": True,
            "binding_matches_current": True,
            "mounted_digest_matches": True,
        }
        if include_application:
            if state.loopback_port is None:
                raise SandboxError("active sandbox has no loopback port")
            status, application = self._http_json(state.loopback_port, "/api/license")
            expected = {
                "valid": not expired,
                "tier": "education",
                "expires_at": expires_at,
                "expired": expired,
                "managed_externally": True,
                "binding_present": True,
                "binding_matches_current": True,
                "license_sha256": digest,
            }
            if status != 200 or any(application.get(key) != value for key, value in expected.items()):
                raise SandboxError("application license observation does not match mounted license")
            result["application_matches"] = True
        return result

    def _http_json(self, port: int, path: str) -> tuple[int, dict[str, object]]:
        if not re.fullmatch(r"/[A-Za-z0-9_/-]+", path):
            raise ValidationError("invalid loopback API path")
        connection = HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            value = json.loads(response.read())
            if not isinstance(value, dict):
                raise SandboxError("loopback API returned a non-object response")
            return response.status, value
        finally:
            connection.close()

    def _api_json(
        self,
        port: int,
        path: str,
        *,
        method: str = "GET",
        form: dict[str, str] | None = None,
        payload: dict[str, object] | None = None,
        token: str = "",
        api_key: str = "",
    ) -> tuple[int, object]:
        if not re.fullmatch(r"/[A-Za-z0-9_/?=&.-]+", path):
            raise ValidationError("invalid loopback API path")
        body: bytes | None = None
        headers: dict[str, str] = {}
        if form is not None:
            body = urllib.parse.urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if api_key:
            headers["X-API-Key"] = api_key
        connection = HTTPConnection("127.0.0.1", port, timeout=15)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            value = json.loads(raw) if raw else None
            return response.status, value
        finally:
            connection.close()

    @staticmethod
    def _read_controller_secret(paths: SandboxPaths, name: str) -> str:
        path = paths.secrets / name
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise SandboxError(f"controller secret {name} is missing or has unsafe permissions")
        value = path.read_text(encoding="utf-8").strip()
        if not value:
            raise SandboxError(f"controller secret {name} is empty")
        return value

    def _verify_personas_and_graph(self, state: LifecycleState) -> dict[str, object]:
        if state.loopback_port is None:
            raise SandboxError("active sandbox has no loopback port")
        self._assert_named_containers_owned(state, allow_absent=False)
        paths = self._paths(state.sandbox_id)
        api_key = self._read_controller_secret(paths, "api-key")
        definitions = {
            "administrator": ("administrator@northstar-lab.example.invalid", "admin-password", "admin"),
            "teacher": ("teacher@northstar-lab.example.invalid", "teacher-password", "operator"),
            "student": ("student@northstar-lab.example.invalid", "student-password", "viewer"),
        }
        identities: dict[str, dict[str, object]] = {}
        tokens: dict[str, str] = {}
        for persona, (email, secret_name, role) in definitions.items():
            password = self._read_controller_secret(paths, secret_name)
            status, login = self._api_json(
                state.loopback_port,
                "/api/auth/login",
                method="POST",
                form={"username": email, "password": password},
            )
            if status != 200 or not isinstance(login, dict) or not isinstance(login.get("access_token"), str):
                raise SandboxError(f"{persona} login failed")
            token = str(login["access_token"])
            tokens[persona] = token
            status, identity = self._api_json(
                state.loopback_port, "/api/auth/me", token=token, api_key=api_key
            )
            if status != 200 or not isinstance(identity, dict):
                raise SandboxError(f"{persona} identity check failed")
            if identity.get("username") != email or identity.get("role") != role or not identity.get("group_id"):
                raise SandboxError(f"{persona} role or tenant binding is incorrect")
            identities[persona] = identity
        group_ids = {identity["group_id"] for identity in identities.values()}
        if len(group_ids) != 1:
            raise SandboxError("personas are not scoped to one organization")

        installation_status, installation = self._api_json(
            state.loopback_port,
            "/api/license/installation-id",
            token=tokens["administrator"],
            api_key=api_key,
        )
        if (
            installation_status != 200
            or not isinstance(installation, dict)
            or installation.get("installation_id") != state.installation_id
        ):
            raise SandboxError("authenticated installation binding proof failed")

        student_token = tokens["student"]
        denied, _ = self._api_json(
            state.loopback_port,
            "/api/models",
            method="POST",
            payload={"name": "forbidden student mutation"},
            token=student_token,
            api_key=api_key,
        )
        if denied != 403:
            raise SandboxError("student write denial was not enforced")
        printer_status, printers = self._api_json(
            state.loopback_port, "/api/printers", token=student_token, api_key=api_key
        )
        if printer_status != 200 or not isinstance(printers, list) or len(printers) != 4:
            raise SandboxError("student cannot see the four-printer school graph")
        by_type = {str(item.get("api_type")): item for item in printers if isinstance(item, dict)}
        if set(by_type) != {"bambu", "elegoo", "moonraker", "prusalink"}:
            raise SandboxError("printer protocol declarations are incomplete")
        bambu = by_type["bambu"]
        if bambu.get("is_active") is not True or bambu.get("has_api_key") is not False or bambu.get("api_host") not in (None, ""):
            raise SandboxError("Bambu replay row must be active and credential/transport free")
        for api_type in ("elegoo", "moonraker", "prusalink"):
            row = by_type[api_type]
            if row.get("is_active") is not False or row.get("has_api_key") is not False or row.get("api_host") not in (None, ""):
                raise SandboxError(f"{api_type} inert transport proof failed")
        telemetry_age: float | None = None
        telemetry_deadline = time.monotonic() + 60
        while time.monotonic() < telemetry_deadline:
            last_seen = bambu.get("last_seen")
            if isinstance(last_seen, str) and last_seen:
                try:
                    seen = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
                    if seen.tzinfo is None:
                        seen = seen.replace(tzinfo=timezone.utc)
                    telemetry_age = (datetime.now(timezone.utc) - seen.astimezone(timezone.utc)).total_seconds()
                except ValueError:
                    telemetry_age = None
                if telemetry_age is not None and 0 <= telemetry_age <= 45:
                    break
            time.sleep(1)
            printer_status, printers = self._api_json(
                state.loopback_port, "/api/printers", token=student_token, api_key=api_key
            )
            if printer_status != 200 or not isinstance(printers, list):
                raise SandboxError("Bambu replay telemetry refresh failed")
            bambu = next(
                (item for item in printers if isinstance(item, dict) and item.get("api_type") == "bambu"),
                {},
            )
        if telemetry_age is None or telemetry_age < 0 or telemetry_age > 45:
            raise SandboxError("Bambu replay did not produce fresh application telemetry")
        inert_logs = self._docker_json(
            ["docker", "exec", "-w", "/app/backend", f"{state.compose_project}-odin-1", "python3", "-c", _INERT_LOG_PROOF_SCRIPT],
            "inert printer connection-attempt proof",
        )
        if inert_logs.get("connection_attempt_markers") != 0:
            raise SandboxError("inert printer monitor logs show a connection attempt")
        report_status, report = self._api_json(
            state.loopback_port,
            "/api/education/usage-report?days=30",
            token=tokens["teacher"],
            api_key=api_key,
        )
        if report_status != 200 or not isinstance(report, dict) or not isinstance(report.get("summary"), dict):
            raise SandboxError("teacher Education usage report is unavailable")
        users_status, users = self._api_json(
            state.loopback_port, "/api/users", token=tokens["administrator"], api_key=api_key
        )
        if users_status != 200 or not isinstance(users, list) or len(users) != 3:
            raise SandboxError("administrator user-management capability failed")
        return {
            "organization_id": next(iter(group_ids)),
            "personas": {name: str(value[2]) for name, value in definitions.items()},
            "student_write_denied": True,
            "printer_protocols": {"bambu": "replay", "elegoo": "inert", "moonraker": "inert", "prusalink": "inert"},
            "printer_count": 4,
            "bambu_telemetry_age_seconds": round(telemetry_age, 3),
            "inert_connection_attempt_markers": 0,
            "education_report": True,
        }

    def _assert_runtime_evidence(self, state: LifecycleState) -> dict[str, object]:
        if state.loopback_port is None:
            raise SandboxError("active sandbox has no loopback port")
        resources = self._assert_state_resource_ownership(state)
        self._assert_named_containers_owned(state, allow_absent=False)
        if not state.lease_expires_at or _lease_deadline(state.lease_expires_at) <= datetime.now(timezone.utc):
            raise SandboxError("active sandbox lease is expired or missing")
        license_evidence = self._observe_bound_license(
            state, resources, include_application=True
        )
        if license_evidence.get("expired") is True:
            raise SandboxError("active sandbox license is expired")
        network_evidence = self._assert_network_isolation(state)
        heartbeat: dict[str, object] | None = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                heartbeat = self._docker_json(
                    ["docker", "run", "--rm", "--name", _helper_container_name(state.compose_project, "heartbeat-read"), "--network", "none",
                     "--label", f"{OWNER_LABEL}={state.sandbox_id}", "--label", f"{SCHEMA_LABEL}=1",
                     "--label", f"{GENERATION_LABEL}={state.reset_generation}",
                     "--label", f"{PROJECT_LABEL}={state.compose_project}",
                     "-v", f"{resources.heartbeat_volume}:/heartbeat:ro", "--entrypoint", "python3", state.candidate_image_id, "-c", _HEARTBEAT_SCRIPT],
                    "Bambu heartbeat",
                )
                break
            except SandboxError:
                time.sleep(1)
        if heartbeat is None:
            raise SandboxError("Bambu replay heartbeat did not become fresh")
        images: dict[str, str] = {}
        for service in ("odin", "publisher", "proxy"):
            container = f"{state.compose_project}-{service}-1"
            image = self._run(["docker", "inspect", "--format", "{{.Image}}", container]).stdout.strip()
            if image != state.candidate_image_id:
                raise SandboxError(f"{service} does not use the exact candidate image")
            images[service] = image
        broker = self._run(
            ["docker", "inspect", "--format", "{{.Image}}", f"{state.compose_project}-mosquitto-1"]
        ).stdout.strip()
        if broker != state.broker_image_id or state.broker_digest != BROKER_IMAGE.split("@sha256:", 1)[-1]:
            raise SandboxError("broker image identity does not match the pinned digest")
        images["mosquitto"] = broker
        return {
            "license": license_evidence,
            "heartbeat": heartbeat,
            "images": images,
            "network_isolation": network_evidence,
        }

    def _recover_interrupted_activation(
        self, paths: SandboxPaths, state: LifecycleState
    ) -> LifecycleState:
        origin_text = state.last_transition.get("from", "")
        if origin_text not in {Phase.PREPARED.value, Phase.EXPIRED.value}:
            raise StateError("ACTIVATING recovery origin is invalid")
        origin = Phase(origin_text)
        resources = self._assert_prepared_identity(state)
        try:
            self._assert_named_containers_owned(state, allow_absent=True)
            self._compose(state, "down", timeout=120)
            if origin == Phase.EXPIRED:
                backup = paths.secrets / PRIOR_LICENSE_FILENAME
                if backup.is_symlink() or not backup.is_file() or backup.stat().st_mode & 0o077:
                    raise SandboxError("interrupted renewal has no safe prior-license backup")
                prior = backup.read_bytes()
                if hashlib.sha256(prior).hexdigest() != state.license_sha256:
                    raise SandboxError("prior-license backup does not match expired state")
                write_secret(paths.secrets / LICENSE_FILENAME, prior, paths.directory)
                self._put_volume_license(state, resources, prior)
                if self._volume_license_digest(state, resources) != state.license_sha256:
                    raise SandboxError("prior expired license restoration failed")
            else:
                (paths.secrets / LICENSE_FILENAME).unlink(missing_ok=True)
                self._remove_volume_license(state, resources)
                state.license_sha256 = ""
                state.license_tier = ""
                state.license_expires_at = ""
                state.binding_present = False
                state.binding_matches_current = False
                state.lease_starts_at = ""
                state.lease_expires_at = ""
            (paths.secrets / PRIOR_LICENSE_FILENAME).unlink(missing_ok=True)
            state.phase = origin
            state.last_transition = {
                "from": Phase.ACTIVATING.value,
                "to": origin.value,
                "at": utc_now(),
                "detail": "interrupted activation recovered with identity and license invariants",
            }
            save_state(paths.state, state, paths.directory)
            return state
        except BaseException:
            state.phase = Phase.DEGRADED
            state.last_transition = {
                "from": Phase.ACTIVATING.value,
                "to": Phase.DEGRADED.value,
                "at": utc_now(),
                "detail": "interrupted activation recovery could not prove cleanup",
            }
            save_state(paths.state, state, paths.directory)
            raise

    def activate(self, sandbox_id: str, stream, *, is_tty: bool, expires_at: str) -> LifecycleState:
        paths = self._paths(sandbox_id)
        state = load_state(paths.state)
        if state.phase == Phase.ACTIVATING:
            state = self._recover_interrupted_activation(paths, state)
        if state.phase not in {Phase.PREPARED, Phase.EXPIRED}:
            raise StateError("activate requires PREPARED or EXPIRED phase")
        raw = read_bounded_stdin(stream, limit=LICENSE_LIMIT, is_tty=is_tty)
        content = _normalized_license(raw)
        lease_deadline = _lease_deadline(expires_at)
        if lease_deadline <= datetime.now(timezone.utc):
            raise ValidationError("lease expiry must be in the future")
        origin_phase = state.phase
        resources = ResourceSet.from_id(sandbox_id)
        self._assert_prepared_identity(state)
        self._activation_request_digest(paths, state)
        prior_license: bytes | None = None
        if origin_phase == Phase.EXPIRED:
            prior_license = self._controller_license_bytes(paths, state.license_sha256)
            if self._volume_license_digest(state, resources) != state.license_sha256:
                raise SandboxError("expired sandbox mounted license does not match state")
            write_secret(paths.secrets / PRIOR_LICENSE_FILENAME, prior_license, paths.directory)
        state.transition(Phase.ACTIVATING, detail="validating signed bound Education license")
        save_state(paths.state, state, paths.directory)
        try:
            evidence = self._validate_license(state, resources, content)
            if not evidence.get("valid"):
                raise SandboxError("license validation failed")
            if evidence.get("tier") != "education":
                raise SandboxError("license tier must be education")
            if evidence.get("binding_present") is not True or evidence.get("binding_matches_current") is not True:
                raise SandboxError("license must match the prepared installation")
            if lease_deadline > _license_deadline(str(evidence.get("expires_at", ""))):
                raise ValidationError("sandbox lease cannot outlive the signed license")
            digest = hashlib.sha256(content).hexdigest()
            if evidence.get("license_sha256") != digest:
                raise SandboxError("candidate license digest does not match controller input")
            write_secret(paths.secrets / LICENSE_FILENAME, content, paths.directory)
            self._put_volume_license(state, resources, content)
            self._compose(state, "up", "-d", timeout=300)
            port_text = self._compose(state, "port", "proxy", "8080").stdout.strip()
            match = _PORT.search(port_text)
            if not match:
                raise SandboxError("Compose did not expose a loopback application port")
            state.loopback_port = int(match.group(1))
            self._wait_ready(state.loopback_port)
            state.license_sha256 = digest
            state.license_tier = "education"
            state.license_expires_at = str(evidence["expires_at"])
            state.binding_present = True
            state.binding_matches_current = True
            state.lease_starts_at = utc_now()
            state.lease_expires_at = lease_deadline.isoformat()
            state.verification = self._assert_runtime_evidence(state)
            state.verification["school_graph"] = self._verify_personas_and_graph(state)
            state.transition(Phase.ACTIVE, detail="application ready with verified Education license")
            save_state(paths.state, state, paths.directory)
            (paths.secrets / PRIOR_LICENSE_FILENAME).unlink(missing_ok=True)
            return state
        except BaseException as activation_exc:
            cleanup_error: BaseException | None = None
            try:
                self._compose(state, "down", timeout=120)
            except BaseException as exc:
                cleanup_error = exc
            try:
                if prior_license is not None:
                    write_secret(paths.secrets / LICENSE_FILENAME, prior_license, paths.directory)
                    self._put_volume_license(state, resources, prior_license)
                    if self._volume_license_digest(state, resources) != state.license_sha256:
                        raise SandboxError("prior expired license restoration failed")
                    (paths.secrets / PRIOR_LICENSE_FILENAME).unlink(missing_ok=True)
                    target = Phase.EXPIRED
                    detail = "renewal failed; prior bound license and expired state preserved"
                else:
                    (paths.secrets / LICENSE_FILENAME).unlink(missing_ok=True)
                    self._remove_volume_license(state, resources)
                    state.license_sha256 = ""
                    state.license_tier = ""
                    state.license_expires_at = ""
                    state.binding_present = False
                    state.binding_matches_current = False
                    state.lease_starts_at = ""
                    state.lease_expires_at = ""
                    target = Phase.PREPARED
                    detail = "activation failed; staged license removed"
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
                target = Phase.DEGRADED
                detail = "activation failed and cleanup could not prove invariants"
            if cleanup_error is not None:
                target = Phase.DEGRADED
                detail = "activation failed and cleanup could not prove invariants"
            state.phase = target
            state.last_transition = {"from": Phase.ACTIVATING.value, "to": target.value, "at": utc_now(), "detail": detail}
            save_state(paths.state, state, paths.directory)
            if cleanup_error is not None:
                raise SandboxError("activation cleanup failed; sandbox is DEGRADED") from activation_exc
            raise

    def status(self, sandbox_id: str) -> dict[str, object]:
        paths = self._paths(sandbox_id)
        if not paths.state.exists():
            tombstone = paths.root / ".tombstones" / f"{validate_sandbox_id(sandbox_id)}.json"
            if tombstone.is_file():
                value = json.loads(tombstone.read_text(encoding="utf-8"))
                if value.get("status") == "PURGING":
                    return {
                        "sandbox_id": sandbox_id,
                        "phase": Phase.PURGING.value,
                        "observed_status": ObservedStatus.DEGRADED.value,
                        "recovery_required": True,
                    }
                evidence = self._verify_purged_tombstone(paths, value)
                return {
                    "sandbox_id": sandbox_id,
                    "phase": Phase.PURGED.value,
                    "observed_status": ObservedStatus.PURGED.value,
                    "absence": evidence,
                }
            if paths.directory.exists() or paths.directory.is_symlink():
                try:
                    _, absence = self._assert_unpublished_prepare(
                        paths, ResourceSet.from_id(paths.directory.name)
                    )
                except SandboxError:
                    return {
                        "sandbox_id": paths.directory.name,
                        "phase": Phase.PREPARING.value,
                        "observed_status": ObservedStatus.DEGRADED.value,
                        "recovery_required": True,
                        "recovery_safe": False,
                    }
                return {
                    "sandbox_id": paths.directory.name,
                    "phase": Phase.PREPARING.value,
                    "observed_status": ObservedStatus.DEGRADED.value,
                    "recovery_required": True,
                    "recovery_safe": True,
                    "absence": absence,
                }
            return {"sandbox_id": validate_sandbox_id(sandbox_id), "phase": "NEW", "observed_status": ObservedStatus.ABSENT.value}
        state = load_state(paths.state)
        observed_license: dict[str, object] | None = None
        resources: ResourceSet | None = None
        try:
            resources = self._assert_state_resource_ownership(state)
            ownership_valid = True
        except (OwnershipError, SandboxError):
            ownership_valid = False
        running = False
        observation_error = False
        prepared_current: dict[str, object] | None = None
        if ownership_valid and state.phase == Phase.PREPARED:
            try:
                prepared_current = self._assert_prepared_current_evidence(state)
            except (OwnershipError, SandboxError, OSError, ValueError, json.JSONDecodeError):
                observation_error = True
        if ownership_valid and state.phase in {Phase.ACTIVE, Phase.EXPIRED}:
            try:
                self._assert_named_containers_owned(state, allow_absent=False)
                running = self._service_running(state, "odin")
                assert resources is not None
                observed_license = self._observe_bound_license(
                    state,
                    resources,
                    include_application=running,
                )
                self._assert_network_isolation(state)
            except (OwnershipError, SandboxError, OSError, ValueError, json.JSONDecodeError):
                observation_error = True
        lease_expired = False
        if state.phase == Phase.ACTIVE:
            try:
                lease_expired = (
                    not state.lease_expires_at
                    or _lease_deadline(state.lease_expires_at) <= datetime.now(timezone.utc)
                )
            except ValidationError:
                observation_error = True
        license_expired = bool(observed_license and observed_license.get("expired") is True)
        deadline_expired = state.phase == Phase.ACTIVE and (lease_expired or license_expired)
        if not ownership_valid or observation_error:
            observed = ObservedStatus.DEGRADED
        elif deadline_expired:
            try:
                stopped = all(
                    not self._service_running(state, service)
                    for service in ("odin", "publisher", "proxy")
                )
            except SandboxError:
                stopped = False
            observed = ObservedStatus.EXPIRED if stopped else ObservedStatus.DEGRADED
        elif state.phase == Phase.EXPIRED:
            try:
                stopped = all(
                    not self._service_running(state, service)
                    for service in ("odin", "publisher", "proxy")
                )
            except SandboxError:
                stopped = False
            observed = ObservedStatus.EXPIRED if stopped else ObservedStatus.DEGRADED
        else:
            container_error = False
            try:
                self._assert_named_containers_owned(
                    state, allow_absent=state.phase != Phase.ACTIVE
                )
                running = self._service_running(state, "odin")
            except SandboxError:
                running = False
                container_error = True
            if container_error:
                observed = ObservedStatus.DEGRADED
            elif running:
                if state.phase == Phase.ACTIVE and state.loopback_port is not None:
                    try:
                        http_status, ready = self._http_json(state.loopback_port, "/health/ready")
                        observed = (
                            ObservedStatus.READY
                            if http_status == 200 and ready.get("ready") is True
                            else ObservedStatus.DEGRADED
                        )
                    except (OSError, SandboxError, ValueError, json.JSONDecodeError):
                        observed = ObservedStatus.STARTING
                else:
                    observed = ObservedStatus.STARTING
            elif state.phase == Phase.DEGRADED:
                observed = ObservedStatus.DEGRADED
            else:
                observed = ObservedStatus.STOPPED
        result: dict[str, object] = {
            "sandbox_id": sandbox_id,
            "phase": state.phase.value,
            "observed_status": observed.value,
            "candidate_image_id": state.candidate_image_id,
            "broker_image_id": state.broker_image_id,
            "broker_digest": state.broker_digest,
            "installation_id": state.installation_id,
            "device_public_key_sha256": state.device_public_key_sha256,
            "license_sha256": state.license_sha256,
            "license_tier": state.license_tier,
            "license_expires_at": state.license_expires_at,
            "observed_license_expires_at": (
                observed_license.get("expires_at") if observed_license else None
            ),
            "observed_license_expired": (
                observed_license.get("expired") if observed_license else None
            ),
            "lease_expires_at": state.lease_expires_at,
            "loopback_port": state.loopback_port,
            "reset_generation": state.reset_generation,
        }
        if prepared_current is not None:
            result["prepared_current"] = prepared_current
        return result

    def _service_running(self, state: LifecycleState, service: str) -> bool:
        result = self._try_run(
            ["docker", "inspect", "--format", "{{.State.Running}}", f"{state.compose_project}-{service}-1"],
            timeout=30,
        )
        if result.returncode == 0:
            return result.stdout.strip() == "true"
        if "no such" in result.stderr.lower() or "not found" in result.stderr.lower():
            return False
        raise SandboxError(f"cannot verify {service} container state")

    def expire(self, sandbox_id: str, *, confirm: str) -> LifecycleState:
        if confirm != sandbox_id:
            raise ValidationError("expire confirmation must exactly match the sandbox ID")
        paths = self._paths(sandbox_id)
        state = load_state(paths.state)
        if state.phase != Phase.ACTIVE:
            raise StateError("expire requires ACTIVE phase")
        state.lease_expires_at = utc_now()
        state.last_transition = {
            "from": Phase.ACTIVE.value,
            "to": Phase.ACTIVE.value,
            "at": utc_now(),
            "detail": "lease deadline moved to current instant; reconcile required",
        }
        save_state(paths.state, state, paths.directory)
        return state

    def reconcile(self, sandbox_id: str) -> LifecycleState:
        paths = self._paths(sandbox_id)
        state = load_state(paths.state)
        if state.phase not in {Phase.ACTIVE, Phase.EXPIRED}:
            raise StateError("reconcile requires ACTIVE or EXPIRED phase")
        try:
            resources = self._assert_state_resource_ownership(state)
            self._assert_named_containers_owned(state, allow_absent=False)
        except BaseException:
            state.transition(
                Phase.DEGRADED,
                detail="expiry reconciliation could not prove resource ownership",
            )
            save_state(paths.state, state, paths.directory)
            raise
        try:
            odin_running = self._service_running(state, "odin")
            license_evidence = self._observe_bound_license(
                state,
                resources,
                include_application=odin_running,
            )
            now = datetime.now(timezone.utc)
            lease_expired = (
                not state.lease_expires_at
                or _lease_deadline(state.lease_expires_at) <= now
            )
            license_expired = license_evidence.get("expired") is True
        except BaseException as observation_exc:
            try:
                self._compose(state, "stop", "publisher", "proxy", "odin", timeout=120)
                if any(
                    self._service_running(state, service)
                    for service in ("odin", "publisher", "proxy")
                ):
                    raise SandboxError("fail-closed license reconciliation left a service running")
            except BaseException as stop_exc:
                state.transition(
                    Phase.DEGRADED,
                    detail="license observation failed and services could not be proven stopped",
                )
                save_state(paths.state, state, paths.directory)
                raise SandboxError(
                    "license observation failed and fail-closed stop was not proven"
                ) from stop_exc
            state.transition(
                Phase.DEGRADED,
                detail="license observation failed; application and simulator stopped fail-closed",
            )
            save_state(paths.state, state, paths.directory)
            raise SandboxError(
                "license observation failed; services were stopped and state is DEGRADED"
            ) from observation_exc
        if state.phase == Phase.ACTIVE and not lease_expired and not license_expired:
            if (
                odin_running
                and self._service_running(state, "publisher")
                and self._service_running(state, "proxy")
            ):
                return state
            try:
                self._compose(state, "stop", "publisher", "proxy", "odin", timeout=120)
            finally:
                state.transition(
                    Phase.DEGRADED,
                    detail="active runtime was incomplete and was stopped fail-closed",
                )
                save_state(paths.state, state, paths.directory)
            raise SandboxError("active sandbox services are incomplete; state is DEGRADED")
        try:
            self._compose(state, "stop", "publisher", "proxy", "odin", timeout=120)
            for service in ("odin", "publisher", "proxy"):
                if self._service_running(state, service):
                    raise SandboxError(f"expired {service} container is still running")
        except BaseException:
            state.transition(Phase.DEGRADED, detail="expiry reconciliation could not prove services stopped")
            save_state(paths.state, state, paths.directory)
            raise
        if state.phase == Phase.ACTIVE:
            state.transition(Phase.EXPIRED, detail="expired application and simulator stopped")
            save_state(paths.state, state, paths.directory)
        return state

    def reset(self, sandbox_id: str, *, confirm: str, recover: bool = False) -> LifecycleState:
        if confirm != sandbox_id:
            raise ValidationError("reset confirmation must exactly match the sandbox ID")
        paths = self._paths(sandbox_id)
        state = load_state(paths.state)
        allowed = {Phase.RESETTING, Phase.DEGRADED} if recover else {Phase.ACTIVE}
        if state.phase not in allowed:
            raise StateError("reset requires ACTIVE, or RESETTING/DEGRADED with --recover")
        recovery_source = state.phase
        # Prove every destructive target and preserved identity before entering
        # RESETTING or mounting the data volume read-write.
        original = self._assert_reset_invariants(state, paths)
        if state.phase == Phase.ACTIVE:
            state.transition(Phase.RESETTING, detail="resetting mutable data while preserving identity")
        else:
            state.phase = Phase.RESETTING
            state.last_transition = {"from": recovery_source.value, "to": "RESETTING", "at": utc_now(), "detail": "operator-authorized recovery reset"}
        save_state(paths.state, state, paths.directory)
        resources = ResourceSet.from_id(sandbox_id)
        try:
            if not recover:
                self._assert_named_containers_owned(state, allow_absent=False)
                sentinel = self._docker_json(
                    ["docker", "exec", "-w", "/app/backend", f"{state.compose_project}-odin-1", "python3", "-c", _CREATE_RESET_SENTINEL_SCRIPT],
                    "reset mutation sentinel",
                )
                if sentinel.get("reset_sentinel_created") is not True:
                    raise SandboxError("reset mutation sentinel creation failed")
            self._compose(state, "down", timeout=120)
            self._run(
                ["docker", "run", "--rm", "--name", _helper_container_name(state.compose_project, "reset-data"), "--network", "none",
                 "--label", f"{OWNER_LABEL}={state.sandbox_id}", "--label", f"{SCHEMA_LABEL}=1",
                 "--label", f"{GENERATION_LABEL}={state.reset_generation + 1}",
                 "--label", f"{PROJECT_LABEL}={state.compose_project}",
                 "-v", f"{resources.data_volume}:/data", "--entrypoint", "python3", state.candidate_image_id, "-c", _RESET_DATA_SCRIPT],
                timeout=120,
            )
            self._compose(state, "up", "-d", timeout=300)
            self._assert_named_containers_owned(
                state,
                allow_absent=False,
                expected_generation=state.reset_generation + 1,
            )
            port_text = self._compose(state, "port", "proxy", "8080").stdout.strip()
            match = _PORT.search(port_text)
            if not match:
                raise SandboxError("reset runtime has no loopback port")
            state.loopback_port = int(match.group(1))
            self._wait_ready(state.loopback_port)
            identity = self._docker_json(
                ["docker", "exec", "-w", "/app/backend", f"{state.compose_project}-odin-1", "python3", "-c", _READ_IDENTITY_SCRIPT],
                "reset identity",
            )
            actual = (
                str(identity.get("installation_id", "")),
                str(identity.get("device_public_key_sha256", "")),
                state.license_sha256,
            )
            if actual != original:
                raise SandboxError("reset changed installation, device, or license identity")
            sentinel = self._docker_json(
                ["docker", "exec", "-w", "/app/backend", f"{state.compose_project}-odin-1", "python3", "-c", _CHECK_RESET_SENTINEL_SCRIPT],
                "reset mutation sentinel removal",
            )
            if sentinel.get("reset_sentinel_removed") is not True:
                raise SandboxError("reset mutation sentinel survived data replacement")
            state.reset_generation += 1
            state.verification = self._assert_runtime_evidence(state)
            state.verification["school_graph"] = self._verify_personas_and_graph(state)
            state.verification["reset_sentinel_removed"] = True
            state.verification["reset_recovery_completed"] = recover
            state.transition(Phase.ACTIVE, detail="mutable data reset and baseline graph restored")
            save_state(paths.state, state, paths.directory)
            return state
        except BaseException:
            state.phase = Phase.DEGRADED
            state.last_transition = {"from": Phase.RESETTING.value, "to": Phase.DEGRADED.value, "at": utc_now(), "detail": "reset failed; operator recovery required"}
            save_state(paths.state, state, paths.directory)
            raise

    def _resource_labels(self, kind: str, name: str) -> dict[str, str] | None:
        command = ["docker"] + (["volume", "inspect"] if kind == "volume" else ["network", "inspect"] if kind == "network" else ["inspect"])
        result = self._try_run([*command, name], timeout=30)
        if result.returncode:
            if "no such" in result.stderr.lower() or "not found" in result.stderr.lower():
                return None
            raise SandboxError(f"cannot verify {kind} resource absence")
        try:
            payload = json.loads(result.stdout)
            labels = payload[0].get("Labels") or payload[0].get("Config", {}).get("Labels") or {}
        except (IndexError, TypeError, json.JSONDecodeError) as exc:
            raise OwnershipError(f"cannot inspect {kind} ownership") from exc
        return {str(key): str(value) for key, value in labels.items()}

    def _labelled_resource_names(self, kind: str, sandbox_id: str) -> list[str]:
        if kind not in {"volume", "network"}:
            raise ValueError("label enumeration supports only volumes and networks")
        result = self._try_run(
            [
                "docker",
                kind,
                "ls",
                "--filter",
                f"label={OWNER_LABEL}={sandbox_id}",
                "--format",
                "{{.Name}}",
            ],
            timeout=30,
        )
        if result.returncode != 0:
            raise SandboxError(f"cannot enumerate sandbox-labelled {kind} resources")
        names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if len(names) != len(set(names)):
            raise SandboxError(f"sandbox-labelled {kind} inventory contains duplicates")
        return sorted(names)

    def _assert_owned(
        self,
        kind: str,
        name: str,
        sandbox_id: str,
        *,
        expected_generation: int | None = None,
    ) -> bool:
        labels = self._resource_labels(kind, name)
        if labels is None:
            return False
        if (
            labels.get(OWNER_LABEL) != sandbox_id
            or labels.get(SCHEMA_LABEL) != "1"
            or labels.get(PROJECT_LABEL) != compose_project(sandbox_id)
        ):
            raise OwnershipError(f"{kind} {name} has foreign or missing ownership labels")
        actual_generation = labels.get(GENERATION_LABEL, "")
        if expected_generation is None:
            generation_valid = bool(re.fullmatch(r"[0-9]+", actual_generation))
        else:
            generation_valid = actual_generation == str(expected_generation)
        if not generation_valid:
            raise OwnershipError(f"{kind} {name} has a stale or missing generation label")
        return True

    def _assert_named_containers_owned(
        self,
        state: LifecycleState,
        *,
        allow_absent: bool,
        services: tuple[str, ...] = ("odin", "publisher", "proxy", "mosquitto"),
        include_prepare: bool = False,
        expected_generation: int | None = None,
    ) -> None:
        resources = ResourceSet.from_id(state.sandbox_id)
        generation = state.reset_generation if expected_generation is None else expected_generation
        names = [f"{state.compose_project}-{service}-1" for service in services]
        for name in names:
            exists = self._assert_owned(
                "container",
                name,
                state.sandbox_id,
                expected_generation=generation,
            )
            if not exists and not allow_absent:
                raise OwnershipError(f"required owned container is absent: {name}")
        if include_prepare:
            for name in (resources.prepare_container, resources.prepare_proxy):
                exists = self._assert_owned(
                    "container",
                    name,
                    state.sandbox_id,
                    expected_generation=0,
                )
                if not exists and not allow_absent:
                    raise OwnershipError(f"required owned container is absent: {name}")

    def _assert_state_resource_ownership(self, state: LifecycleState) -> ResourceSet:
        resources = ResourceSet.from_id(state.sandbox_id)
        if state.resources != resources.state_value() or state.compose_project != resources.project:
            raise OwnershipError("persisted resource identities do not match the sandbox ID")
        if not _IMAGE_ID.fullmatch(state.candidate_image_id):
            raise OwnershipError("persisted candidate image identity is invalid")
        for volume in (resources.data_volume, resources.secret_volume, resources.heartbeat_volume):
            if not self._assert_owned(
                "volume", volume, state.sandbox_id, expected_generation=0
            ):
                raise OwnershipError(f"required owned volume is absent: {volume}")
        for network in (resources.internal_network, resources.edge_network):
            if not self._assert_owned(
                "network", network, state.sandbox_id, expected_generation=0
            ):
                raise OwnershipError(f"required owned network is absent: {network}")
        return resources

    def _recover_interrupted_prepare(
        self, paths: SandboxPaths, resources: ResourceSet
    ) -> None:
        if not paths.state.is_file():
            raise StateError("prepare recovery requires readable persisted state")
        state = load_state(paths.state)
        failed_prepare = state.phase == Phase.PREPARING or (
            state.phase == Phase.DEGRADED
            and state.last_transition.get("from") == Phase.PREPARING.value
        )
        if not failed_prepare:
            raise StateError("prepare recovery is limited to interrupted PREPARING state")
        if state.resources != resources.state_value() or state.compose_project != resources.project:
            raise OwnershipError("interrupted prepare resources do not match sandbox identity")
        if state.candidate_tag != f"odin-edu-candidate:{state.sandbox_id}":
            raise OwnershipError("interrupted prepare candidate tag is invalid")
        if any(path.is_symlink() for path in paths.directory.rglob("*")):
            raise OwnershipError("interrupted prepare directory contains a symlink")

        self._assert_named_containers_owned(state, allow_absent=True, include_prepare=True)
        resource_errors = self._cleanup_failed_prepare_resources(state, resources)
        if resource_errors:
            raise SandboxError(
                "interrupted prepare resource cleanup failed: "
                + "; ".join(resource_errors)
            )

        if paths.image_journal.exists() or paths.image_journal.is_symlink():
            journal = _load_image_journal(paths.image_journal)
            if journal.get("candidate_tag") != state.candidate_tag:
                raise OwnershipError("image ownership journal candidate tag changed")
            if journal["capture_status"] == "pending":
                protected = journal["protected_history"]
                assert isinstance(protected, dict)
                lifecycle = DisposableImageLifecycle(
                    state.candidate_tag,
                    command=self._image_command,
                    owner_token=str(journal["owner_token"]),
                )
                lifecycle.lock = acquire_image_lifecycle_lock()
                lifecycle.capacity = dict(journal["capacity"])  # type: ignore[arg-type]
                lifecycle.protected = ProtectedImageHistory(
                    visible_ids=(),
                    protected_ids=tuple(str(item) for item in protected["ids"]),
                    protected_sha256=str(protected["sha256"]),
                )
                lifecycle.build_attempted = True
                iid_error: BaseException | None = None
                try:
                    image_id = self._read_private_iid(paths)
                    if verify_built_image(
                        state.candidate_tag,
                        paths.image_iid,
                        command=self._image_command,
                    ) != image_id:
                        raise OwnershipError("recovered build IID changed")
                    lifecycle.establish_final_image(image_id)
                    journal["final_image_id"] = image_id
                    _private_json(paths.image_journal, journal)
                except BaseException as exc:
                    iid_error = exc
                    journal["final_image_id"] = None
                    _private_json(paths.image_journal, journal)
                try:
                    lifecycle.freeze_ownership()
                    journal = self._succeeded_image_journal(
                        lifecycle, paths, journal
                    )
                    paths.image_iid.unlink()
                    cleanup_errors = lifecycle.finalize()
                    journal = self._record_lifecycle_cleanup(
                        paths, journal, lifecycle, cleanup_errors
                    )
                except BaseException as exc:
                    if (
                        lifecycle.lock is not None
                        and journal.get("capture_status") == "succeeded"
                    ):
                        cleanup_errors = lifecycle.finalize()
                    elif lifecycle.lock is not None:
                        cleanup_errors = [
                            "image ownership capture was not durably journaled; "
                            "no image deletion issued"
                        ]
                        cleanup_errors.extend(
                            release_image_lifecycle_lock(lifecycle.lock)
                        )
                        lifecycle.lock = None
                    else:
                        cleanup_errors = list(lifecycle.cleanup_errors or [])
                    journal["recovery_error"] = type(exc).__name__
                    journal["cleanup_errors"] = cleanup_errors
                    _private_json(paths.image_journal, journal)
                    raise OwnershipError(
                        "pending image ownership capture could not be completed safely"
                    ) from exc
                if cleanup_errors:
                    raise SandboxError(
                        "recovered image cleanup failed: "
                        + "; ".join(cleanup_errors)
                    )
                if iid_error is not None:
                    journal["recovery_error"] = type(iid_error).__name__
                    _private_json(paths.image_journal, journal)
                    raise OwnershipError(
                        "pending ownership history was cleaned but image tag "
                        "authority could not be established"
                    ) from iid_error
            else:
                _journal, cleanup_errors = self._cleanup_fixed_image_journal(
                    paths, journal
                )
                if cleanup_errors:
                    raise SandboxError(
                        "recovered image cleanup failed: "
                        + "; ".join(cleanup_errors)
                    )
        else:
            # Legacy/incomplete states have no deletion authority. Recovery is
            # safe only when the exact candidate tag is already absent.
            observed = self._try_run(
                [
                    "docker",
                    "image",
                    "inspect",
                    "--format",
                    "{{.Id}}",
                    state.candidate_tag,
                ],
                timeout=30,
            )
            if observed.returncode == 0:
                raise OwnershipError(
                    "interrupted prepare has an image tag but no ownership journal"
                )
        self._docker_absence_evidence(
            state.sandbox_id, candidate_tag=state.candidate_tag, loopback_port=None
        )
        for path in sorted(paths.directory.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()
        paths.directory.rmdir()

    def _assert_unpublished_prepare(
        self, paths: SandboxPaths, resources: ResourceSet
    ) -> tuple[list[Path], dict[str, object]]:
        """Validate the crash window before the first state.json publication."""
        if not paths.directory.is_dir() or paths.directory.is_symlink():
            raise OwnershipError("unpublished prepare directory is not a safe directory")
        if resources != ResourceSet.from_id(paths.directory.name):
            raise OwnershipError("unpublished prepare resources do not match sandbox identity")
        remnants = list(paths.directory.iterdir())
        for path in remnants:
            if (
                path.is_symlink()
                or not path.is_file()
                or path.parent != paths.directory
                or not path.name.startswith(".state-")
            ):
                raise OwnershipError(
                    "unpublished prepare contains unexpected controller data"
                )
        absence = self._docker_absence_evidence(
            paths.directory.name,
            candidate_tag=f"odin-edu-candidate:{paths.directory.name}",
            loopback_port=None,
        )
        return remnants, absence

    @staticmethod
    def _remove_unpublished_prepare(paths: SandboxPaths, remnants: list[Path]) -> None:
        for path in remnants:
            path.unlink()
        paths.directory.rmdir()

    def _docker_absence_evidence(
        self,
        sandbox_id: str,
        *,
        candidate_tag: str,
        loopback_port: int | None,
    ) -> dict[str, object]:
        resources = ResourceSet.from_id(sandbox_id)
        container_result = self._try_run(
            ["docker", "ps", "-aq", "--filter", f"label={OWNER_LABEL}={sandbox_id}"],
            timeout=30,
        )
        if container_result.returncode != 0:
            raise SandboxError("cannot verify labelled container absence")
        containers = [item for item in container_result.stdout.splitlines() if item]
        labelled_volumes = self._labelled_resource_names("volume", sandbox_id)
        labelled_networks = self._labelled_resource_names("network", sandbox_id)
        named_residue: list[str] = []
        for name in (
            resources.prepare_container,
            resources.prepare_proxy,
            f"{resources.project}-odin-1",
            f"{resources.project}-publisher-1",
            f"{resources.project}-proxy-1",
            f"{resources.project}-mosquitto-1",
        ):
            if self._resource_labels("container", name) is not None:
                named_residue.append(f"container:{name}")
        for kind, name in (
            ("volume", resources.data_volume),
            ("volume", resources.secret_volume),
            ("volume", resources.heartbeat_volume),
            ("network", resources.internal_network),
            ("network", resources.edge_network),
        ):
            if self._resource_labels(kind, name) is not None:
                named_residue.append(f"{kind}:{name}")
        image_result = self._try_run(
            ["docker", "image", "inspect", candidate_tag], timeout=30
        )
        if image_result.returncode == 0:
            image_tag_present = True
        elif "no such" in image_result.stderr.lower() or "not found" in image_result.stderr.lower():
            image_tag_present = False
        else:
            raise SandboxError("cannot verify candidate image-tag absence")
        port_open = False
        if isinstance(loopback_port, int) and 0 < loopback_port <= 65535:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.25)
                port_open = probe.connect_ex(("127.0.0.1", loopback_port)) == 0
        if (
            containers
            or labelled_volumes
            or labelled_networks
            or named_residue
            or image_tag_present
            or port_open
        ):
            raise SandboxError("purged sandbox still has container, resource, image-tag, or port residue")
        return {
            "labelled_containers_absent": True,
            "labelled_volumes_absent": True,
            "labelled_networks_absent": True,
            "expected_named_containers_absent": True,
            "named_volumes_absent": True,
            "named_networks_absent": True,
            "candidate_tag_absent": True,
            "license_mount_absent": True,
            "heartbeat_volume_absent": True,
            "loopback_port_closed": True,
        }

    def _verify_purged_tombstone(
        self, paths: SandboxPaths, value: dict[str, object]
    ) -> dict[str, object]:
        if value.get("sandbox_id") != paths.directory.name or value.get("status") != "PASS":
            raise StateError("purge tombstone identity or status is invalid")
        expected_tag = f"odin-edu-candidate:{paths.directory.name}"
        if value.get("candidate_tag") != expected_tag:
            raise StateError("purge tombstone candidate tag is invalid")
        if paths.directory.exists() or paths.directory.is_symlink():
            raise SandboxError("purged controller directory still exists")
        html = paths.root / ".tombstones" / f"{paths.directory.name}.html"
        if not html.is_file():
            raise SandboxError("purge HTML tombstone is missing")
        evidence = self._docker_absence_evidence(
            paths.directory.name,
            candidate_tag=expected_tag,
            loopback_port=value.get("loopback_port") if isinstance(value.get("loopback_port"), int) else None,
        )
        return {**evidence, "controller_directory_absent": True, "html_tombstone_present": True}

    def _finish_purge_tombstone(
        self, paths: SandboxPaths, journal: dict[str, object]
    ) -> dict[str, object]:
        sandbox_id = paths.directory.name
        expected_tag = f"odin-edu-candidate:{sandbox_id}"
        removed = journal.get("removed")
        if (
            journal.get("schema_version") != 1
            or journal.get("sandbox_id") != sandbox_id
            or journal.get("status") not in {"PURGING", "PASS"}
            or journal.get("candidate_tag") != expected_tag
            or (
                journal.get("candidate_image_id") != ""
                and not _IMAGE_ID.fullmatch(str(journal.get("candidate_image_id", "")))
            )
            or not isinstance(removed, dict)
            or any(
                not isinstance(removed.get(key), list)
                or any(not isinstance(item, str) for item in removed.get(key, []))
                for key in ("containers", "volumes", "networks", "image_tags")
            )
        ):
            raise StateError("interrupted purge journal is invalid")
        if paths.state.exists():
            raise StateError("cannot finalize purge while controller state exists")
        if paths.directory.exists():
            if paths.image_journal.exists() or paths.image_journal.is_symlink():
                ownership = _load_image_journal(paths.image_journal)
                expected_digest = journal.get("image_ownership_journal_sha256")
                if (
                    not isinstance(expected_digest, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", expected_digest)
                    or _journal_digest(paths.image_journal) != expected_digest
                    or ownership.get("capture_status") != "succeeded"
                ):
                    raise StateError("purge ownership-journal recovery is invalid")
                cleanup = ownership.get("cleanup")
                assert isinstance(cleanup, dict)
                history = cleanup.get("history")
                owned_history = ownership.get("owned_history_ids")
                if (
                    cleanup.get("tag")
                    not in {"removed", "already_absent", "unauthorized"}
                    or not isinstance(history, list)
                    or not isinstance(owned_history, list)
                    or len(history) != len(owned_history)
                    or any(
                        record.get("status") not in {"REMOVED", "ALREADY_ABSENT"}
                        for record in history
                        if isinstance(record, dict)
                    )
                ):
                    raise StateError("purge ownership cleanup is incomplete")
                history_errors = verify_owned_image_history_absent(
                    tuple(str(item) for item in owned_history),
                    command=self._image_command,
                )
                if history_errors:
                    raise StateError(
                        "purge ownership history remains: "
                        + "; ".join(history_errors)
                    )
                paths.image_journal.unlink()
            remnants, _ = self._assert_unpublished_prepare(
                paths, ResourceSet.from_id(sandbox_id)
            )
            self._remove_unpublished_prepare(paths, remnants)

        loopback_port = journal.get("loopback_port")
        docker_absence = self._docker_absence_evidence(
            sandbox_id,
            candidate_tag=expected_tag,
            loopback_port=loopback_port if isinstance(loopback_port, int) else None,
        )
        final_absence = {
            **docker_absence,
            "controller_directory_absent": True,
            "html_tombstone_present": True,
        }
        result = {
            **journal,
            "status": "PASS",
            "purged_at": str(journal.get("purged_at") or utc_now()),
            "residue": [],
            "absence": final_absence,
        }
        tombstone_dir = paths.root / ".tombstones"
        tombstone = tombstone_dir / f"{sandbox_id}.json"
        published_html = tombstone.with_suffix(".html")
        if tombstone_dir.is_symlink():
            raise OwnershipError("purge tombstone directory cannot be a symlink")
        tombstone_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- sanitized tombstones remain controller-owned; directories need execute permission and 0644 is invalid
        os.chmod(tombstone_dir, 0o700)
        from .report import render_report

        report_manifest = {
            "status": "PASS",
            "sandbox_id": sandbox_id,
            "run_id": f"purge-{sandbox_id}",
            "source_commit": str(result.get("source_commit", "")),
            "candidate_image_id": str(result["candidate_image_id"]),
            "summary": "Label-owned containers, volumes, networks, secret files, heartbeat data, and loopback runtime were removed.",
            "phases": [
                {
                    "name": "purge-absence",
                    "status": "PASS",
                    "detail": (
                        f"containers={len(removed['containers'])}, "
                        f"volumes={len(removed['volumes'])}, "
                        f"networks={len(removed['networks'])}, "
                        f"image_tags={len(removed['image_tags'])}, residue=0"
                    ),
                }
            ],
            "evidence": {"removed": removed, "absence": final_absence},
        }
        token = random_secrets.token_hex(8)
        staged_json = tombstone_dir / f".{sandbox_id}-{token}.json.tmp"
        staged_html = tombstone_dir / f".{sandbox_id}-{token}.html.tmp"
        try:
            _public_json(staged_json, result)
            render_report(report_manifest, staged_html)
            os.replace(staged_html, published_html)
            try:
                os.replace(staged_json, tombstone)
            except BaseException:
                published_html.unlink(missing_ok=True)
                raise
        finally:
            staged_json.unlink(missing_ok=True)
            staged_html.unlink(missing_ok=True)
        result["absence"] = self._verify_purged_tombstone(paths, result)
        return result

    def _assert_reset_invariants(
        self, state: LifecycleState, paths: SandboxPaths
    ) -> tuple[str, str, str]:
        resources = self._assert_state_resource_ownership(state)
        if not re.fullmatch(r"[0-9a-f]{64}", state.license_sha256):
            raise OwnershipError("reset requires a verified persisted license digest")
        self._controller_license_bytes(paths, state.license_sha256)
        if self._volume_license_digest(state, resources) != state.license_sha256:
            raise OwnershipError("mounted license identity changed before reset")
        identity = self._data_identity(state, resources)
        actual = (
            str(identity.get("installation_id", "")),
            str(identity.get("device_public_key_sha256", "")),
            state.license_sha256,
        )
        expected = (state.installation_id, state.device_public_key_sha256, state.license_sha256)
        if actual != expected:
            raise OwnershipError("installation, device, or license identity changed before reset")
        return expected

    def purge(self, sandbox_id: str, *, confirm: str) -> dict[str, object]:
        sandbox_id = validate_sandbox_id(sandbox_id)
        if confirm != sandbox_id:
            raise ValidationError("purge confirmation must exactly match the sandbox ID")
        paths = self._paths(sandbox_id)
        resources = ResourceSet.from_id(sandbox_id)
        tombstone_dir = paths.root / ".tombstones"
        tombstone = tombstone_dir / f"{sandbox_id}.json"
        if tombstone_dir.is_symlink():
            raise OwnershipError("purge tombstone directory cannot be a symlink")
        if not paths.state.exists():
            if tombstone.is_file():
                value = json.loads(tombstone.read_text(encoding="utf-8"))
                if value.get("status") == "PURGING":
                    return self._finish_purge_tombstone(paths, value)
                try:
                    value["absence"] = self._verify_purged_tombstone(paths, value)
                    return value
                except SandboxError:
                    return self._finish_purge_tombstone(paths, value)
            if paths.directory.exists() or paths.directory.is_symlink():
                remnants, docker_absence = self._assert_unpublished_prepare(
                    paths, resources
                )
                journal = {
                    "schema_version": 1,
                    "sandbox_id": sandbox_id,
                    "status": "PURGING",
                    "source_commit": "",
                    "purged_at": "",
                    "candidate_image_id": "",
                    "candidate_tag": f"odin-edu-candidate:{sandbox_id}",
                    "loopback_port": None,
                    "installation_id_sha256": "",
                    "device_public_key_sha256": "",
                    "license_sha256": "",
                    "removed": {
                        "containers": [],
                        "volumes": [],
                        "networks": [],
                        "image_tags": [],
                    },
                    "residue": [],
                    "absence": docker_absence,
                }
                tombstone_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- purge journals remain controller-owned; directories need execute permission and 0644 is invalid
                os.chmod(tombstone_dir, 0o700)
                _public_json(tombstone, journal)
                self._remove_unpublished_prepare(paths, remnants)
                return self._finish_purge_tombstone(paths, journal)
            raise StateError("sandbox state does not exist")
        state = load_state(paths.state)
        if state.resources != resources.state_value() or state.compose_project != resources.project:
            raise OwnershipError("persisted resource identities do not match the sandbox ID")
        expected_candidate_tag = f"odin-edu-candidate:{sandbox_id}"
        if state.candidate_tag != expected_candidate_tag:
            raise OwnershipError("persisted candidate tag does not match the sandbox ID")
        if state.candidate_image_id and not _IMAGE_ID.fullmatch(state.candidate_image_id):
            raise OwnershipError("persisted candidate image identity is invalid")
        # Validate the complete controller tree before any Docker observation
        # or mutation so hostile links cannot influence a bounded purge.
        if any(path.is_symlink() for path in paths.directory.rglob("*")):
            raise OwnershipError("sandbox directory contains a symlink during purge")
        image_ownership_journal: dict[str, object] | None = None
        if paths.image_journal.exists() or paths.image_journal.is_symlink():
            image_ownership_journal = _load_image_journal(paths.image_journal)
            if image_ownership_journal.get("capture_status") != "succeeded":
                raise OwnershipError("purge requires a succeeded image ownership capture")
            if image_ownership_journal.get("candidate_tag") != state.candidate_tag:
                raise OwnershipError("purge image ownership tag changed")
            journal_image_id = image_ownership_journal.get("final_image_id")
            if state.candidate_image_id and journal_image_id != state.candidate_image_id:
                raise OwnershipError("purge image ownership identity changed")
            actual_journal_digest = _journal_digest(paths.image_journal)
            if state.image_ownership_journal_sha256:
                if actual_journal_digest != state.image_ownership_journal_sha256:
                    raise OwnershipError("purge image ownership journal digest changed")
            elif state.phase not in {Phase.PREPARING, Phase.DEGRADED, Phase.PURGING}:
                raise OwnershipError("sandbox image lease is missing its journal digest")
        elif state.image_ownership_journal_sha256:
            raise OwnershipError("sandbox image ownership journal is missing")
        else:
            legacy_image = self._try_run(
                [
                    "docker",
                    "image",
                    "inspect",
                    "--format",
                    "{{.Id}}",
                    state.candidate_tag,
                ],
                timeout=30,
            )
            legacy_output = (legacy_image.stdout + legacy_image.stderr).lower()
            if legacy_image.returncode == 0:
                raise OwnershipError(
                    "legacy sandbox image tag has no bounded deletion authority"
                )
            if "no such" not in legacy_output and "not found" not in legacy_output:
                raise SandboxError("legacy sandbox image absence could not be proven")
        self._assert_named_containers_owned(state, allow_absent=True, include_prepare=True)
        if state.phase != Phase.PURGING:
            state.transition(Phase.PURGING, detail="bounded purge started")
            save_state(paths.state, state, paths.directory)
        container_ids = self._try_run(
            ["docker", "ps", "-aq", "--filter", f"label={OWNER_LABEL}={sandbox_id}"], timeout=30
        ).stdout.splitlines()
        removed: dict[str, list[str]] = {"containers": [], "volumes": [], "networks": [], "image_tags": []}
        for container_id in filter(None, container_ids):
            name = self._run(["docker", "inspect", "--format", "{{.Name}}", container_id]).stdout.strip().lstrip("/")
            if not name.startswith(resources.project + "-"):
                raise OwnershipError("label-matched container has a foreign name")
            self._assert_owned("container", container_id, sandbox_id)
            self._run(["docker", "rm", "-f", container_id], timeout=60)
            removed["containers"].append(name)
        for kind, expected, removed_key in (
            (
                "volume",
                {resources.data_volume, resources.secret_volume, resources.heartbeat_volume},
                "volumes",
            ),
            (
                "network",
                {resources.internal_network, resources.edge_network},
                "networks",
            ),
        ):
            labelled = set(self._labelled_resource_names(kind, sandbox_id))
            for name in sorted(expected | labelled):
                if not name.startswith(resources.project + "-"):
                    raise OwnershipError(
                        f"sandbox-labelled {kind} has a foreign resource name"
                    )
                if self._assert_owned(kind, name, sandbox_id, expected_generation=0):
                    self._run(["docker", kind, "rm", name], timeout=60)
                    removed[removed_key].append(name)
        image_cleanup_evidence: dict[str, object] | None = None
        if image_ownership_journal is not None:
            image_ownership_journal, image_cleanup_errors = (
                self._cleanup_fixed_image_journal(
                    paths, image_ownership_journal
                )
            )
            if image_cleanup_errors:
                raise SandboxError(
                    "purge image cleanup failed: " + "; ".join(image_cleanup_errors)
                )
            image_cleanup_evidence = dict(
                image_ownership_journal.get("cleanup", {})
            )
            if image_cleanup_evidence.get("tag") == "removed":
                removed["image_tags"].append(state.candidate_tag)
        residue = []
        for kind, name in (
            ("volume", resources.data_volume), ("volume", resources.secret_volume),
            ("volume", resources.heartbeat_volume), ("network", resources.internal_network),
            ("network", resources.edge_network),
        ):
            if self._resource_labels(kind, name) is not None:
                residue.append(f"{kind}:{name}")
        if residue:
            raise SandboxError("purge left Docker residue")
        docker_absence = self._docker_absence_evidence(
            sandbox_id,
            candidate_tag=state.candidate_tag,
            loopback_port=state.loopback_port,
        )
        tombstone_ownership_digest = (
            _journal_digest(paths.image_journal)
            if image_ownership_journal is not None
            else ""
        )
        journal = {
            "schema_version": 1,
            "sandbox_id": sandbox_id,
            "status": "PURGING",
            "source_commit": state.source_commit,
            "purged_at": "",
            "candidate_image_id": state.candidate_image_id,
            "candidate_tag": state.candidate_tag,
            "loopback_port": state.loopback_port,
            "installation_id_sha256": hashlib.sha256(state.installation_id.encode()).hexdigest(),
            "device_public_key_sha256": state.device_public_key_sha256,
            "license_sha256": state.license_sha256,
            "removed": removed,
            "image_ownership_cleanup": image_cleanup_evidence,
            "image_ownership_journal_sha256": tombstone_ownership_digest,
            "residue": [],
            "absence": docker_absence,
        }
        tombstone_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- purge journals remain controller-owned; directories need execute permission and 0644 is invalid
        os.chmod(tombstone_dir, 0o700)
        _public_json(tombstone, journal)
        for path in sorted(
            paths.directory.rglob("*"),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            if path in {paths.state, paths.image_journal}:
                continue
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()
        paths.state.unlink()
        paths.image_journal.unlink(missing_ok=True)
        paths.directory.rmdir()
        return self._finish_purge_tombstone(paths, journal)
