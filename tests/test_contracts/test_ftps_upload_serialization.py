"""One Bambu FTPS upload at a time; an upload failure never fences the printer.

specs/ftps-upload-serialization.md. On 2026-11-08 an H2D upload overlapping a slow
X1C upload timed out mid-transfer, and the claim was fenced 'uncertain' although
no print command had been sent.
"""
import subprocess
import sys
import threading
import time
from types import SimpleNamespace as NS

import pytest

from modules.printers import dispatch
from modules.printers.telemetry.bambu import ftp_upload
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401 (fixture)


@pytest.fixture(autouse=True)
def lock_path(tmp_path, monkeypatch):
    path = tmp_path / "locks" / "bambu-ftps-upload.lock"
    monkeypatch.setenv("ODIN_FTPS_UPLOAD_LOCK_PATH", str(path))
    monkeypatch.delenv("ODIN_FTPS_UPLOAD_SERIAL", raising=False)
    monkeypatch.delenv("ODIN_FTPS_UPLOAD_WAIT_SECONDS", raising=False)
    return path


def _recording_upload(monkeypatch, hold=0.3, outcome=(True, "done")):
    spans, active, peak = [], [0], [0]
    guard = threading.Lock()

    def fake(*args):
        with guard:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        start = time.monotonic()
        time.sleep(hold)
        with guard:
            active[0] -= 1
        spans.append((start, time.monotonic()))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(ftp_upload, "_upload_once", fake)
    return spans, peak


def _run_threads(count):
    results = [None] * count

    def run(i):
        results[i] = ftp_upload.upload_file("h", "c", "/inert", f"f{i}.3mf")

    threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    return results


def test_threads_never_overlap_and_the_second_waits(monkeypatch):
    spans, peak = _recording_upload(monkeypatch)
    results = _run_threads(3)
    assert all(results) and peak[0] == 1
    assert sorted(r.phase for r in results) == ["done"] * 3
    assert max(r.waited_seconds for r in results) > 0.2


def test_serial_off_allows_overlap(monkeypatch):
    monkeypatch.setenv("ODIN_FTPS_UPLOAD_SERIAL", "0")
    spans, peak = _recording_upload(monkeypatch)
    assert all(_run_threads(3)) and peak[0] > 1


def test_other_process_holding_the_slot_times_out_without_connecting(monkeypatch, lock_path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen([sys.executable, "-c", (
        "import fcntl,os,sys,time\n"
        "fd=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT,0o600)\n"
        "fcntl.flock(fd,fcntl.LOCK_EX)\n"
        "print('held',flush=True)\n"
        "time.sleep(5)\n"), str(lock_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        monkeypatch.setenv("ODIN_FTPS_UPLOAD_WAIT_SECONDS", "0.6")
        calls = []
        monkeypatch.setattr(ftp_upload, "_upload_once", lambda *a: calls.append(a) or (True, "done"))
        notices = []
        with ftp_upload.wait_notice(lambda: notices.append(1)):
            monkeypatch.setattr(ftp_upload, "WAIT_NOTICE_SECONDS", 0.1)
            result = ftp_upload.upload_file("h", "c", "/inert", "f.3mf")
        assert not result and result.phase == "timeout" and calls == []
        assert notices == [1]
    finally:
        holder.kill()
        holder.wait()
    # The kernel released the dead holder's lock.
    monkeypatch.setattr(ftp_upload, "_upload_once", lambda *a: (True, "done"))
    assert ftp_upload.upload_file("h", "c", "/inert", "f.3mf")


@pytest.mark.parametrize("phase", ["connect", "authentication", "protection", "transfer", "logout"])
def test_slot_released_after_failure_in_every_phase(monkeypatch, phase):
    monkeypatch.setattr(ftp_upload, "_upload_once", lambda *a: (False, phase))
    first = ftp_upload.upload_file("h", "c", "/inert", "f.3mf")
    assert not first and first.phase == phase
    monkeypatch.setenv("ODIN_FTPS_UPLOAD_WAIT_SECONDS", "0")
    monkeypatch.setattr(ftp_upload, "_upload_once", lambda *a: (True, "done"))
    assert ftp_upload.upload_file("h", "c", "/inert", "f.3mf")


def test_slot_released_when_the_upload_raises(monkeypatch):
    _recording_upload(monkeypatch, hold=0, outcome=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        ftp_upload.upload_file("h", "c", "/inert", "f.3mf")
    monkeypatch.setenv("ODIN_FTPS_UPLOAD_WAIT_SECONDS", "0")
    monkeypatch.setattr(ftp_upload, "_upload_once", lambda *a: (True, "done"))
    assert ftp_upload.upload_file("h", "c", "/inert", "f.3mf")


def test_unopenable_lock_refuses_before_connecting(monkeypatch, tmp_path):
    """Codex P2-3: a broken lock path never silently restores concurrent uploads."""
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("ODIN_FTPS_UPLOAD_LOCK_PATH", str(blocker / "nested" / "lock"))
    calls = []
    monkeypatch.setattr(ftp_upload, "_upload_once", lambda *a: calls.append(a) or (True, "done"))
    result = ftp_upload.upload_file("h", "c", "/inert", "f.3mf")
    assert not result and result.phase == "lock_unavailable" and calls == []
    monkeypatch.setenv("ODIN_FTPS_UPLOAD_SERIAL", "0")  # the only bypass
    assert ftp_upload.upload_file("h", "c", "/inert", "f.3mf")


def test_waiters_beyond_the_cap_refuse_immediately(monkeypatch):
    """Codex P2-2: queued uploads cannot occupy every dispatch worker."""
    monkeypatch.setenv("ODIN_FTPS_UPLOAD_MAX_WAITERS", "1")
    spans, peak = _recording_upload(monkeypatch, hold=0.6)
    started = time.monotonic()
    results = _run_threads(3)
    phases = sorted(r.phase for r in results)
    assert phases == ["busy", "done", "done"] and peak[0] == 1
    busy = [r for r in results if r.phase == "busy"][0]
    assert busy.waited_seconds == 0.0 and time.monotonic() - started < 2.5


def test_slot_acquired_after_the_deadline_is_not_used(monkeypatch, lock_path):
    """Codex P2-5: a scheduler delay carries the waiter past its deadline; the slot
    it then acquires is given back unused and nothing is uploaded."""
    import fcntl
    monkeypatch.setenv("ODIN_FTPS_UPLOAD_WAIT_SECONDS", "0.6")
    now = [0.0]
    real_flock = fcntl.flock
    acquired_late = []
    def flock(fd, op):
        if op & fcntl.LOCK_EX:
            if now[0] < 0.75:
                raise BlockingIOError()
            acquired_late.append(now[0])
        return real_flock(fd, op)
    monkeypatch.setattr(ftp_upload.fcntl, "flock", flock)
    def delayed_sleep(seconds):
        now[0] += seconds + 0.45  # the thread wakes well after it asked to
    calls = []
    monkeypatch.setattr(ftp_upload, "_upload_once", lambda *a: calls.append(a) or (True, "done"))
    with ftp_upload.upload_slot(clock=lambda: now[0], sleep=delayed_sleep) as (state, waited):
        assert state == "timeout" and waited > 0.6
    assert acquired_late, "the test must reach a late acquisition"
    assert calls == []
    # Released: another descriptor can take it without waiting.
    import os
    fd = os.open(str(lock_path), os.O_RDWR)
    try:
        real_flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        real_flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def test_two_processes_upload_one_after_the_other(lock_path, tmp_path):
    """Spec: two real processes both succeed and their transfers never overlap."""
    import json
    import os
    script = (
        "import json,sys,time\n"
        "from modules.printers.telemetry.bambu import ftp_upload\n"
        "def once(*a):\n"
        "    start=time.time(); time.sleep(0.5); return True, 'done'\n"
        "spans=[]\n"
        "def once_recorded(*a):\n"
        "    start=time.time(); r=once(*a); spans.append((start,time.time())); return r\n"
        "ftp_upload._upload_once=once_recorded\n"
        "r=ftp_upload.upload_file('h','c','/inert','f.3mf')\n"
        "print(json.dumps({'ok':bool(r),'phase':r.phase,'spans':spans}))\n")
    env = dict(os.environ, ODIN_FTPS_UPLOAD_LOCK_PATH=str(lock_path), PYTHONPATH="backend:.")
    procs = [subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True, env=env) for _ in range(2)]
    results = [json.loads(p.communicate(timeout=20)[0].strip().splitlines()[-1]) for p in procs]
    assert all(r["ok"] and r["phase"] == "done" for r in results)
    (a0, a1), (b0, b1) = results[0]["spans"][0], results[1]["spans"][0]
    assert a1 <= b0 or b1 <= a0

@pytest.mark.parametrize("raw,expected", [("bad", 900.0), ("-5", 0.0), ("99999", 3600.0), ("nan", 900.0), ("12", 12.0)])
def test_wait_limit_is_bounded(monkeypatch, raw, expected):
    monkeypatch.setenv("ODIN_FTPS_UPLOAD_WAIT_SECONDS", raw)
    assert ftp_upload._wait_limit() == expected


def test_legacy_adapter_passes_the_failure_phase_through(monkeypatch):
    """Codex P2-4: legacy dispatch learns where the upload failed."""
    from modules.printers.adapters.bambu import BambuPrinter
    monkeypatch.setattr(ftp_upload, "_upload_once", lambda *a: (False, "connect"))
    printer = BambuPrinter.__new__(BambuPrinter)
    printer.ip, printer.access_code = "h", "c"
    result = printer.upload_file("/inert", "f.3mf")
    assert not result and result.phase == "connect"


# Part B: an upload failure proves no command was sent.

def _bambu_doubles(monkeypatch, upload_result):
    from modules.printers.telemetry.bambu import session, broker_policy
    from modules.printers.adapters import bambu
    commands = []
    monkeypatch.setattr(dispatch, "_ws", lambda *a: None)
    monkeypatch.setattr(dispatch.time, "sleep", lambda *a: None)
    monkeypatch.setattr(ftp_upload, "upload_file", lambda *a: upload_result)
    monkeypatch.setattr(broker_policy, "resolve_bambu_broker_config", lambda *a, **k: NS(host="h", port=8883, use_tls=True))
    monkeypatch.setattr(session, "run_command", lambda *a, **k: commands.append(a) or True)
    monkeypatch.setattr(bambu, "BambuPrinter", lambda **k: NS(
        upload_file=lambda *a: upload_result, connect=lambda: commands.append("connect") or True,
        start_print=lambda *a, **k: commands.append("start") or True, disconnect=lambda: None))
    return commands


@pytest.mark.parametrize("protocol", ["v2", "legacy"])
@pytest.mark.parametrize("phase", ["timeout", "busy", "lock_unavailable", "connect", "authentication", "protection", "transfer", "logout"])
def test_upload_failure_returns_no_start_message_and_never_starts(monkeypatch, protocol, phase):
    commands = _bambu_doubles(monkeypatch, ftp_upload.UploadResult(False, phase, 900.0))
    method = dispatch._dispatch_bambu_v2 if protocol == "v2" else dispatch._dispatch_bambu_legacy
    ok, message = method(1, "/inert", "f.3mf", {"ip": "h", "serial": "s", "access_code": "c"})
    assert ok is False and isinstance(message, dispatch.NoStartMessage)
    assert "no print command was sent" in message or "no file and no print command were sent" in message
    assert commands == []
    assert "c" not in message.split() and "s" not in message.split()


def test_slot_messages_say_nothing_was_sent():
    message = dispatch._upload_failure_message(ftp_upload.UploadResult(False, "timeout", 900.0))
    assert "15 min" in message and "no file and no print command were sent" in message
    for phase in ("busy", "lock_unavailable"):
        assert "no file and no print command were sent" in dispatch._upload_failure_message(
            ftp_upload.UploadResult(False, phase))


def test_plain_bool_failure_from_a_double_is_still_no_start():
    assert isinstance(dispatch._upload_failure_message(False), dispatch.NoStartMessage)


def test_string_operations_drop_the_no_start_marker():
    message = dispatch._upload_failure_message(ftp_upload.UploadResult(False, "transfer"))
    assert type(message + "; Education reservation requires explicit reconciliation") is str
    assert type(message + "; AMS mapping derived from verified file source") is str


def test_failure_after_upload_is_not_marked_no_start(monkeypatch):
    commands = _bambu_doubles(monkeypatch, ftp_upload.UploadResult(True, "done"))
    from modules.printers.telemetry.bambu import session
    monkeypatch.setattr(session, "run_command", lambda *a, **k: False)
    # A failed command after a good upload stays uncertain, as before.
    with pytest.raises(dispatch.DispatchOutcomeUncertain):
        dispatch._dispatch_bambu_v2(1, "/inert", "f.3mf", {"ip": "h", "serial": "s", "access_code": "c"})


def test_run_admitted_dispatch_passes_the_marker_through(monkeypatch):
    monkeypatch.setattr(dispatch, "_ws", lambda *a: None)
    monkeypatch.setattr(dispatch, "_notify_start_outcome", lambda *a, **k: None)
    marker = dispatch._upload_failure_message(ftp_upload.UploadResult(False, "transfer"))
    monkeypatch.setattr(dispatch, "_dispatch_bambu", lambda *a, **k: (False, marker))
    gate = NS(refusal=None)
    ok, message = dispatch._run_admitted_dispatch({"id": 1, "printer_id": 9, "item_name": "t"},
        {"api_type": "bambu", "ip": "h"}, "", "", {}, None, None, before_start=gate)
    assert ok is False and isinstance(message, dispatch.NoStartMessage)


def test_orchestration_releases_claim_only_for_the_marker():
    """Source pin: the release decision reads the marker before any suffix is added."""
    import inspect
    from modules.printers import dispatch_orchestration
    source = inspect.getsource(dispatch_orchestration)
    marker_check = source.index("isinstance(result[1], dispatch.NoStartMessage)")
    suffix = source.index("'; AMS mapping derived from verified file source'")
    assert marker_check < suffix
    assert "getattr(before_start, 'clean_no_start', False) or no_command" in source


# Behavioral: the real admission/claim path (Codex P2-6).

@pytest.fixture
def bambu_dispatch(review_db, tmp_path, monkeypatch):
    from tests.test_contracts.test_dispatch_real_monitor_correlation import configure_dispatch
    engine, _ = configure_dispatch(review_db, tmp_path, monkeypatch, protocol='bambu')
    return engine


def _claims(engine):
    from sqlalchemy import text
    with engine.connect() as conn:
        return conn.execute(text('SELECT job_id, state FROM printer_dispatch_claims')).all()


def test_failed_upload_releases_the_claim_and_the_printer_can_retry(bambu_dispatch, monkeypatch):
    calls = []
    def adapter(job_id, path, name, creds, **options):
        calls.append(name)
        if len(calls) == 1:
            return False, dispatch._upload_failure_message(ftp_upload.UploadResult(False, "transfer"))
        return True, "Synthetic accepted"
    monkeypatch.setattr(dispatch, '_dispatch_bambu', adapter)
    ok, message = dispatch.dispatch_job(59, 53)
    assert not ok and "no print command was sent" in message
    assert _claims(bambu_dispatch) == []
    from sqlalchemy import text
    with bambu_dispatch.begin() as conn:
        conn.execute(text("UPDATE jobs SET status='scheduled' WHERE id=53"))
    assert dispatch.dispatch_job(59, 53)[0] and len(calls) == 2


def test_plain_failure_after_upload_still_fences(bambu_dispatch, monkeypatch):
    monkeypatch.setattr(dispatch, '_dispatch_bambu', lambda *a, **k: (False, "MQTT print command failed (file uploaded OK)"))
    assert not dispatch.dispatch_job(59, 53)[0]
    assert [state for _, state in _claims(bambu_dispatch)] == ['uncertain']


def test_maintenance_hold_set_during_upload_keeps_the_fence(bambu_dispatch, monkeypatch):
    from modules.printers import maintenance_gate
    held = []
    real = maintenance_gate.refusal
    monkeypatch.setattr(maintenance_gate, 'refusal', lambda engine, pid: 'Synthetic hold' if held else real(engine, pid))
    def adapter(*a, **k):
        held.append(1)
        return False, dispatch._upload_failure_message(ftp_upload.UploadResult(False, "connect"))
    monkeypatch.setattr(dispatch, '_dispatch_bambu', adapter)
    assert not dispatch.dispatch_job(59, 53)[0]
    assert [state for _, state in _claims(bambu_dispatch)] == ['uncertain']


def test_failed_education_cancel_strips_the_marker(monkeypatch):
    monkeypatch.setattr(dispatch, "_ws", lambda *a: None)
    monkeypatch.setattr(dispatch, "_notify_start_outcome", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "_cancel_education_reservation", lambda *a: False)
    marker = dispatch._upload_failure_message(ftp_upload.UploadResult(False, "transfer"))
    monkeypatch.setattr(dispatch, "_dispatch_bambu", lambda *a, **k: (False, marker))
    ok, message = dispatch._run_admitted_dispatch({"id": 1, "printer_id": 9, "item_name": "t"},
        {"api_type": "bambu", "ip": "h"}, "", "", {}, object(), {"token": "r"}, before_start=NS(refusal=None))
    assert ok is False and type(message) is str and "explicit reconciliation" in message


def _real_bambu(monkeypatch, protocol, upload_result, *, disconnect_error=False):
    """Real _dispatch_bambu (V2 or legacy) with only the network edges replaced."""
    from modules.printers.telemetry import feature_flag
    from modules.printers.adapters import bambu
    from modules.printers.telemetry.bambu import session
    uploads = []
    monkeypatch.setattr(feature_flag, "is_v2_enabled", lambda: protocol == "v2")
    def upload(*a):
        uploads.append(a)
        return upload_result
    monkeypatch.setattr(ftp_upload, "upload_file", upload)
    monkeypatch.setattr(session, "run_command", lambda *a, **k: pytest.fail("start_print reached"))
    def disconnect():
        if disconnect_error:
            raise RuntimeError("synthetic cleanup failure")
    monkeypatch.setattr(bambu, "BambuPrinter", lambda **k: NS(
        upload_file=lambda *a: upload(*a), connect=lambda: pytest.fail("MQTT reached"),
        start_print=lambda *a, **k: pytest.fail("start_print reached"), disconnect=disconnect))
    return uploads


@pytest.mark.parametrize("protocol", ["v2", "legacy"])
def test_real_dispatcher_upload_failure_releases_and_retry_is_admitted(bambu_dispatch, monkeypatch, protocol):
    uploads = _real_bambu(monkeypatch, protocol, ftp_upload.UploadResult(False, "transfer"))
    ok, message = dispatch.dispatch_job(59, 53)
    assert not ok and "no print command was sent" in message, message
    assert _claims(bambu_dispatch) == []
    from sqlalchemy import text
    with bambu_dispatch.begin() as conn:
        conn.execute(text("UPDATE jobs SET status='scheduled' WHERE id=53"))
    dispatch.dispatch_job(59, 53)
    assert len(uploads) == 2, "the retry must be admitted and reach the upload again"


def test_real_legacy_cleanup_exception_keeps_the_fence(bambu_dispatch, monkeypatch):
    _real_bambu(monkeypatch, "legacy", ftp_upload.UploadResult(False, "connect"), disconnect_error=True)
    assert not dispatch.dispatch_job(59, 53)[0]
    assert [state for _, state in _claims(bambu_dispatch)] == ["uncertain"]


@pytest.mark.parametrize("protocol", ["v2", "legacy"])
def test_real_dispatcher_maintenance_read_error_keeps_the_fence(bambu_dispatch, monkeypatch, protocol):
    from modules.printers import maintenance_gate
    uploads = _real_bambu(monkeypatch, protocol, ftp_upload.UploadResult(False, "transfer"))
    real = maintenance_gate.refusal
    def refusal(engine, pid):
        if uploads:
            raise RuntimeError("synthetic maintenance read failure")
        return real(engine, pid)
    monkeypatch.setattr(maintenance_gate, "refusal", refusal)
    assert not dispatch.dispatch_job(59, 53)[0]
    assert [state for _, state in _claims(bambu_dispatch)] == ["uncertain"]
