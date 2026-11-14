"""ITAR mode, go2rtc in its own container (the enterprise layout): a restart boundary.

ODIN does not start or stop that container, and go2rtc keeps what it loaded in memory. A
camera outside the local network, saved before ITAR mode was turned on, stays reachable
through go2rtc after the configuration file is fixed. Removing streams one by one through
go2rtc's API is not enough (go2rtc v1.9.4 drops the registry entry without stopping the
producer), and neither is comparing what its producers report with what was configured.

So ODIN makes go2rtc start again from the file it has just checked, and proves that it did:

  1. The start-up preparation writes the checked configuration with one extra, empty
     stream whose name is new for this start (the mark), and records the names it wrote
     in a gate file beside the camera relay secret, state "pending". (`open_gate`)
  2. `pass_gate_now` sends POST /api/restart to the running go2rtc, which in v1.9.4
     replaces its own process (internal/api/api.go, restartHandler: syscall.Exec), waits
     for it to answer again, and reads GET /api/streams. After a restart that list is
     exactly the `streams` section of the file go2rtc loaded (internal/streams/streams.go,
     Init). The mark must be in it, which proves the answering process loaded THIS file and
     is not the old process still answering, and every other name must be one ODIN wrote.
     Then the gate file says "passed". A timeout, an error answer, a TLS failure, rejected
     credentials or an unexpected name is a failure.
  3. Only a refused connection (ECONNREFUSED) means "go2rtc is not running": it then holds
     nothing in memory. The gate stays "pending", and under ITAR mode every request ODIN
     makes to go2rtc first calls `require_gate`, from go2rtc_access.go2rtc_target (the one
     place every go2rtc client gets its address), which performs step 2 on the first
     contact. Until the gate has passed, ODIN asks go2rtc for nothing else.

The gate file is shared by every ODIN process and container (it lives with the per-boot
camera relay secret on /data). With ITAR mode off, or go2rtc beside the API in one
container (supervisord starts it afresh from the checked file), none of this runs.

Requests go to the address go2rtc_target checked (local network only, pinned to one
lookup, no proxy), follow no redirect, and carry the per-boot credentials go2rtc may be
running with (this boot's, then the previous boot's). ODIN writes nothing through go2rtc's
API, so a read-only configuration mount in the go2rtc container does not matter.
"""
import contextlib
import errno
import fcntl
import json
import logging
import os
import secrets
import time

import httpx

log = logging.getLogger("odin.go2rtc")
REQUEST_TIMEOUT_SECONDS = 5
RESTART_WAIT_SECONDS = 20       # how long go2rtc may take to answer again with the mark
POLL_SECONDS = 0.25
RETRY_AFTER_FAILURE_SECONDS = 30
MARK_PREFIX = "odin_boot_check_"
_sleep = time.sleep
_clock = time.monotonic
_last_failure = {"at": None, "reason": ""}


class Go2rtcGateFailed(Exception):
    """go2rtc is running but could not be shown to run the checked configuration."""


class Go2rtcNotRunning(Exception):
    """Nothing is listening at go2rtc's address (connection refused)."""


def new_mark() -> str:
    return MARK_PREFIX + secrets.token_hex(8)


def gate_path() -> str:
    from modules.printers.camera_relay import _secret_path
    return _secret_path() + ".go2rtc-itar-gate.json"


def _read_gate():
    try:
        with open(gate_path()) as stream:
            value = json.load(stream)
    except (OSError, ValueError):
        return None
    if (isinstance(value, dict) and isinstance(value.get("mark"), str) and value["mark"].startswith(MARK_PREFIX)
            and isinstance(value.get("names"), list) and value.get("state") in ("pending", "passed")):
        return value
    return None


def _write_gate(value: dict) -> None:
    path = gate_path()
    temporary = f"{path}.{os.getpid()}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@contextlib.contextmanager
def _locked():
    with open(gate_path() + ".lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def open_gate(names, mark: str) -> None:
    """Start-up preparation: the file now holds exactly `names` plus `mark`; nothing has been
    proven about the running go2rtc yet."""
    with _locked():
        _write_gate({"mark": mark, "names": sorted(set(names) | {mark}), "state": "pending"})
    _last_failure["at"] = None


def _refused(error: BaseException) -> bool:
    """True only for a refused TCP connection: ECONNREFUSED somewhere in the cause chain.
    A TLS failure, a timeout, an unreachable network or a name problem is not one."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, ConnectionRefusedError) or getattr(error, "errno", None) == errno.ECONNREFUSED:
            return True
        error = error.__cause__ or error.__context__
    return False


def _call(method, go2rtc, path):
    """One request to go2rtc with each credential it may be running with. Returns the
    response (never a 401). Raises httpx.HTTPError, or Go2rtcGateFailed when every
    credential is rejected."""
    from modules.printers.go2rtc_access import go2rtc_restart_credential_candidates
    send = httpx.post if method == "POST" else httpx.get
    settings = {**go2rtc.client_kwargs, "trust_env": False, "timeout": REQUEST_TIMEOUT_SECONDS,
                "follow_redirects": False}
    for auth in go2rtc_restart_credential_candidates():
        response = send(f"{go2rtc.url}{path}", **{**settings, "auth": auth})
        if response.status_code != 401:
            return response
    raise Go2rtcGateFailed("go2rtc rejected every ODIN credential")


def pass_gate_now() -> None:
    """Step 2: restart the running go2rtc, wait for it, check the names it loaded, and mark
    the gate passed. Raises Go2rtcNotRunning (connection refused: the gate stays pending)
    or Go2rtcGateFailed (anything else)."""
    from modules.printers.go2rtc_access import go2rtc_target
    with _locked():
        gate = _read_gate()
        if gate is None:
            raise Go2rtcGateFailed("there is no record of a checked camera configuration; restart the ODIN API")
        if gate["state"] == "passed":
            return
        try:
            go2rtc = go2rtc_target(_gate=False)   # refused under ITAR mode unless on the local network
        except Exception as exc:
            raise Go2rtcGateFailed("the go2rtc address is not usable") from exc
        try:
            answer = _call("POST", go2rtc, "/api/restart")
        except (httpx.RemoteProtocolError, httpx.ReadError):
            answer = None   # the process was replaced before it finished answering: the mark decides
        except httpx.HTTPError as exc:
            if _refused(exc):
                raise Go2rtcNotRunning() from exc
            raise Go2rtcGateFailed(f"go2rtc could not be asked to restart ({type(exc).__name__})") from exc
        if answer is not None and answer.status_code != 200:
            raise Go2rtcGateFailed(f"go2rtc answered {answer.status_code} to the restart request")
        allowed, deadline, last = set(gate["names"]), _clock() + RESTART_WAIT_SECONDS, "it did not answer"
        while _clock() < deadline:
            _sleep(POLL_SECONDS)
            try:
                listing = _call("GET", go2rtc, "/api/streams")
            except httpx.HTTPError as exc:
                last = f"it did not answer ({type(exc).__name__})"   # restarting: wait for it
                continue
            if listing.status_code != 200:
                raise Go2rtcGateFailed(f"go2rtc answered {listing.status_code} to the stream list")
            try:
                names = listing.json()
            except ValueError as exc:
                raise Go2rtcGateFailed("go2rtc's stream list is not readable") from exc
            if not isinstance(names, dict):
                raise Go2rtcGateFailed("go2rtc's stream list is not readable")
            if gate["mark"] not in names:
                last = "it still answers with the configuration from before the restart"
                continue
            unexpected = sorted(set(names) - allowed)
            if unexpected:
                raise Go2rtcGateFailed(f"go2rtc loaded {len(unexpected)} camera stream(s) ODIN did not write")
            _write_gate({**gate, "state": "passed"})
            log.info("go2rtc restarted with the checked camera configuration")
            return
        raise Go2rtcGateFailed(f"go2rtc did not come back with the checked configuration: {last}")


def require_gate() -> None:
    """Called before every request to a separate go2rtc under ITAR mode. Returns when the
    gate has passed (performing step 2 first if it is still pending). Otherwise raises
    Go2rtcNotRunning or Go2rtcGateFailed and the caller sends nothing. After a failure the
    restart is not tried again for RETRY_AFTER_FAILURE_SECONDS, so a go2rtc that cannot be
    verified is not restarted on every camera request."""
    gate = _read_gate()
    if gate is not None and gate["state"] == "passed":
        return
    failed_at = _last_failure["at"]
    if failed_at is not None and _clock() - failed_at < RETRY_AFTER_FAILURE_SECONDS:
        raise Go2rtcGateFailed(_last_failure["reason"])
    try:
        pass_gate_now()
    except Go2rtcNotRunning:
        raise
    except Go2rtcGateFailed as exc:
        _last_failure.update(at=_clock(), reason=str(exc))
        log.error("go2rtc is not asked for anything: %s", exc)
        raise
    _last_failure["at"] = None
