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
        assert not result and result.phase == "wait" and calls == []
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


def test_unwritable_lock_location_uploads_unserialized(monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("ODIN_FTPS_UPLOAD_LOCK_PATH", str(blocker / "nested" / "lock"))
    monkeypatch.setattr(ftp_upload, "_upload_once", lambda *a: (True, "done"))
    assert ftp_upload.upload_file("h", "c", "/inert", "f.3mf")


@pytest.mark.parametrize("raw,expected", [("bad", 900.0), ("-5", 0.0), ("99999", 3600.0), ("nan", 900.0), ("12", 12.0)])
def test_wait_limit_is_bounded(monkeypatch, raw, expected):
    monkeypatch.setenv("ODIN_FTPS_UPLOAD_WAIT_SECONDS", raw)
    assert ftp_upload._wait_limit() == expected


def test_legacy_adapter_keeps_its_bool_contract(monkeypatch):
    from modules.printers.adapters.bambu import BambuPrinter
    monkeypatch.setattr(ftp_upload, "_upload_once", lambda *a: (False, "connect"))
    printer = BambuPrinter.__new__(BambuPrinter)
    printer.ip, printer.access_code = "h", "c"
    assert printer.upload_file("/inert", "f.3mf") is False


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
        upload_file=lambda *a: bool(upload_result), connect=lambda: commands.append("connect") or True,
        start_print=lambda *a, **k: commands.append("start") or True, disconnect=lambda: None))
    return commands


@pytest.mark.parametrize("protocol", ["v2", "legacy"])
@pytest.mark.parametrize("phase", ["wait", "connect", "authentication", "protection", "transfer", "logout"])
def test_upload_failure_returns_no_start_message_and_never_starts(monkeypatch, protocol, phase):
    commands = _bambu_doubles(monkeypatch, ftp_upload.UploadResult(False, phase, 900.0))
    method = dispatch._dispatch_bambu_v2 if protocol == "v2" else dispatch._dispatch_bambu_legacy
    ok, message = method(1, "/inert", "f.3mf", {"ip": "h", "serial": "s", "access_code": "c"})
    assert ok is False and isinstance(message, dispatch.NoStartMessage)
    assert "no print command was sent" in message or "no file and no print command were sent" in message
    assert commands == []
    assert "c" not in message.split() and "s" not in message.split()


def test_wait_message_names_minutes():
    message = dispatch._upload_failure_message(ftp_upload.UploadResult(False, "wait", 900.0))
    assert "15 min" in message and "you can retry" in message


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
