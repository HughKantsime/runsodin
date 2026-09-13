"""Closed active-exercise state machine; live transports are separate and opt-in."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from pathlib import Path
from typing import Callable, Protocol

from .authorization import ACTION_TABLE, consume_authorization


class ExerciseError(RuntimeError):
    pass


TRANSITION_TIMEOUT_SECONDS = 30.0
TRANSITION_POLL_SECONDS = 0.25


@dataclass(frozen=True)
class Observation:
    state: str
    filename: str = ""
    job_id: int | str | None = None
    ams_slots: int | None = None


class ActiveBackend(Protocol):
    def close(self) -> None: ...
    def observe(self) -> Observation: ...
    def upload(self, remote_name: str) -> bool: ...
    def start(self, remote_name: str) -> bool: ...
    def upload_start(self, remote_name: str) -> bool: ...
    def pause(self, job_id: int | str | None) -> bool: ...
    def resume(self, job_id: int | str | None) -> bool: ...
    def stop(self, job_id: int | str | None) -> bool: ...
    def cancel(self, job_id: int | str | None) -> bool: ...
    def read_ams(self) -> int | None: ...


@dataclass
class ExerciseContext:
    protocol: str
    nonce: str
    backend: ActiveBackend
    remote_name: str
    uploaded: bool = False
    created_job: bool = False
    job_id: int | str | None = None
    elegoo_filename_salt: str | None = None
    elegoo_filename_sha256: str | None = None
    results: list[dict[str, str]] = field(default_factory=list)


def canonical_remote_name(protocol: str, nonce: str) -> str:
    if len(nonce) != 32 or any(char not in "0123456789abcdef" for char in nonce):
        raise ExerciseError("authorization nonce is not filename-safe")
    extension = {"bambu": "3mf", "moonraker": "gcode", "prusalink": "gcode", "elegoo": "ctb"}[protocol]
    return f"ODIN-CERT-{nonce}.{extension}"


def _exact_filename(observed: str, expected: str) -> bool:
    return bool(observed) and PurePosixPath(observed).name == expected


def _identity_matches(context: ExerciseContext, observation: Observation) -> bool:
    if context.protocol == "elegoo":
        if not context.elegoo_filename_salt or not context.elegoo_filename_sha256 or not observation.filename:
            return False
        digest = hashlib.sha256((context.elegoo_filename_salt + observation.filename).encode("utf-8")).hexdigest()
        return digest == context.elegoo_filename_sha256
    if not context.created_job or not _exact_filename(observation.filename, context.remote_name):
        return False
    if context.protocol == "prusalink":
        return context.job_id is not None and observation.job_id == context.job_id
    return observation.job_id == context.job_id


def _require_state(observation: Observation, *states: str) -> None:
    if observation.state not in states:
        raise ExerciseError("active action pre-state is not allowed")


def _require_transition(
    context: ExerciseContext, expected: set[str], intermediate: set[str],
) -> Observation:
    deadline = time.monotonic() + TRANSITION_TIMEOUT_SECONDS
    while True:
        observed = context.backend.observe()
        if observed.state in expected:
            return observed
        if observed.state not in intermediate:
            raise ExerciseError("active action entered an unexpected state")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ExerciseError("active action transition timed out")
        time.sleep(min(TRANSITION_POLL_SECONDS, remaining))


def _upload(context: ExerciseContext) -> None:
    _require_state(context.backend.observe(), "idle")
    if not context.backend.upload(context.remote_name):
        raise ExerciseError("upload transport did not acknowledge")
    context.uploaded = True


def _start(context: ExerciseContext) -> None:
    _require_state(context.backend.observe(), "idle")
    if not context.uploaded or not context.backend.start(context.remote_name):
        raise ExerciseError("start requires same-run upload")
    observed = _require_transition(
        context, {"printing"}, {"idle", "preparing", "heating", "busy"},
    )
    if not _exact_filename(observed.filename, context.remote_name):
        raise ExerciseError("started job identity does not match")
    context.created_job = True
    context.job_id = observed.job_id


def _upload_start(context: ExerciseContext) -> None:
    _require_state(context.backend.observe(), "idle")
    if not context.backend.upload_start(context.remote_name):
        raise ExerciseError("atomic upload-start did not acknowledge")
    observed = _require_transition(
        context, {"printing"}, {"idle", "preparing", "heating", "busy"},
    )
    if not _exact_filename(observed.filename, context.remote_name):
        raise ExerciseError("started job identity does not match")
    if observed.job_id is None:
        raise ExerciseError("PrusaLink job ID was not observed")
    context.uploaded = context.created_job = True
    context.job_id = observed.job_id


def _pause(context: ExerciseContext) -> None:
    before = context.backend.observe()
    _require_state(before, "printing")
    if not _identity_matches(context, before) or not context.backend.pause(before.job_id):
        raise ExerciseError("pause target identity or transport failed")
    after = _require_transition(context, {"paused"}, {"printing", "pausing", "busy"})
    if not _identity_matches(context, after):
        raise ExerciseError("paused job identity changed")


def _resume(context: ExerciseContext) -> None:
    before = context.backend.observe()
    _require_state(before, "paused")
    if not _identity_matches(context, before) or not context.backend.resume(before.job_id):
        raise ExerciseError("resume target identity or transport failed")
    after = _require_transition(context, {"printing"}, {"paused", "resuming", "busy"})
    if not _identity_matches(context, after):
        raise ExerciseError("resumed job identity changed")


def _stop(context: ExerciseContext) -> None:
    before = context.backend.observe()
    _require_state(before, "printing", "paused")
    if not _identity_matches(context, before) or not context.backend.stop(before.job_id):
        raise ExerciseError("stop target identity or transport failed")
    _require_transition(
        context, {"idle", "stopped", "finished"},
        {"printing", "paused", "stopping", "busy"},
    )


def _cancel(context: ExerciseContext) -> None:
    before = context.backend.observe()
    _require_state(before, "printing", "paused")
    if not _identity_matches(context, before) or not context.backend.cancel(before.job_id):
        raise ExerciseError("cancel target identity or transport failed")
    _require_transition(
        context, {"idle", "stopped", "finished"},
        {"printing", "paused", "stopping", "busy"},
    )


def _ams_read(context: ExerciseContext) -> None:
    if context.backend.read_ams() is None:
        raise ExerciseError("AMS observation was unavailable")


DISPATCH = {
    ("bambu", "upload"): _upload, ("bambu", "start"): _start,
    ("bambu", "pause"): _pause, ("bambu", "resume"): _resume,
    ("bambu", "stop"): _stop, ("bambu", "ams_read"): _ams_read,
    ("moonraker", "upload"): _upload, ("moonraker", "start"): _start,
    ("moonraker", "pause"): _pause, ("moonraker", "resume"): _resume,
    ("moonraker", "cancel"): _cancel,
    ("prusalink", "upload_start"): _upload_start, ("prusalink", "pause"): _pause,
    ("prusalink", "resume"): _resume, ("prusalink", "stop"): _stop,
    ("elegoo", "pause"): _pause, ("elegoo", "resume"): _resume,
    ("elegoo", "stop"): _stop,
}


def run_exercise(
    protocol: str, nonce: str, actions: list[str], backend: ActiveBackend, *,
    elegoo_filename_salt: str | None = None, elegoo_filename_sha256: str | None = None,
    result_sink: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    if protocol not in ACTION_TABLE or not actions or len(actions) != len(set(actions)):
        raise ExerciseError("active exercise scope is invalid")
    if any(action not in ACTION_TABLE[protocol] for action in actions):
        raise ExerciseError("active exercise action is not allowlisted")
    context = ExerciseContext(
        protocol=protocol, nonce=nonce, backend=backend,
        remote_name=canonical_remote_name(protocol, nonce),
        elegoo_filename_salt=elegoo_filename_salt,
        elegoo_filename_sha256=elegoo_filename_sha256,
    )
    if result_sink is not None:
        context.results = result_sink
    for action in actions:
        try:
            DISPATCH[(protocol, action)](context)
            context.results.append({"action": action, "status": "pass"})
        except Exception:
            context.results.append({"action": action, "status": "fail"})
            break
    return context.results


def execute_authorized(
    *, authorization_path: Path, target_path: Path, ledger_path: Path,
    backend_factory: Callable[[dict, dict], ActiveBackend], now=None,
    expected_commit: str | None = None,
    artifact_root: Path | None = None,
    result_sink: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    """Consume authorization before constructing any connection-capable backend."""
    authorization, target = consume_authorization(
        authorization_path, target_path=target_path, ledger_path=ledger_path,
        now=now, expected_commit=expected_commit,
        **({"artifact_root": artifact_root} if artifact_root is not None else {}),
    )
    protocol = authorization["protocol"]
    actions = [item["name"] for item in authorization["actions"]]
    backend = backend_factory(target, authorization)
    primary_failure: BaseException | None = None
    try:
        return run_exercise(
            protocol, authorization["nonce"], actions, backend,
            elegoo_filename_salt=authorization.get("elegoo_filename_salt"),
            elegoo_filename_sha256=authorization.get("elegoo_filename_sha256"),
            result_sink=result_sink,
        )
    except BaseException as exc:
        primary_failure = exc
        raise
    finally:
        try:
            backend.close()
        except BaseException:
            if primary_failure is None:
                raise
