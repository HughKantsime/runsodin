"""Readiness refusals name why, without changing the decision."""
import time

import pytest

from modules.printers import dispatch_acquisition as acquisition
from modules.printers import dispatch_orchestration as orchestration


@pytest.mark.parametrize("creds,deadline,code", [
    ({"api_type": "anycubic_lan"}, lambda: time.monotonic() + 5, "unsupported_printer"),
    ({"api_type": "bambu"}, lambda: True, "invalid_deadline"),
    ({"api_type": "bambu"}, lambda: time.monotonic() - 1, "deadline_elapsed"),
])
def test_early_refusals_name_their_reason(creds, deadline, code):
    with acquisition.capture_refusal_reason() as reasons:
        assert acquisition.bounded_status_is_idle(creds, deadline()) is False
    assert reasons == [code]


def test_reason_outside_capture_is_not_recorded_and_capture_resets():
    assert acquisition.bounded_status_is_idle({"api_type": "x"}, 0) is False
    with acquisition.capture_refusal_reason() as outer:
        with acquisition.capture_refusal_reason() as inner:
            acquisition.bounded_status_is_idle({"api_type": "x"}, 0)
        acquisition.bounded_status_is_idle({"api_type": "bambu"}, True)
    assert inner == ["unsupported_printer"] and outer == ["invalid_deadline"]


class _NoReply:
    def __init__(self, result=None, poll=True):
        self.result, self._poll = result, poll
    def Pipe(self, duplex=False):
        context = self
        class End:
            def poll(self, timeout): return context._poll
            def recv(self): return context.result
            def close(self): pass
        return End(), End()
    def Process(self, target, args, daemon):
        class P:
            exitcode = 0
            def start(self): pass
            def join(self, timeout=None): pass
            def is_alive(self): return False
        return P()


@pytest.mark.parametrize("context,code", [
    (_NoReply(poll=False), "no_reply_before_deadline"),
    (_NoReply(result="read_error:TimeoutError"), "read_error:TimeoutError"),
    (_NoReply(result=False), "printer_not_ready"),
    (_NoReply(result="x" * 200), "printer_not_ready"),
])
def test_worker_outcomes_name_their_reason(monkeypatch, context, code):
    monkeypatch.setattr(acquisition, "_spawn_context", lambda: context)
    monkeypatch.setattr(acquisition, "_cleanup_owned_process", lambda p: True)
    with acquisition.capture_refusal_reason() as reasons:
        ok = acquisition.bounded_status_is_idle({"api_type": "bambu", "ip": "h"}, time.monotonic() + 5)
    assert ok is False and reasons == [code]


def test_a_reason_string_is_never_accepted_as_ready(monkeypatch):
    monkeypatch.setattr(acquisition, "_spawn_context", lambda: _NoReply(result="True"))
    monkeypatch.setattr(acquisition, "_cleanup_owned_process", lambda p: True)
    assert acquisition.bounded_status_is_idle({"api_type": "bambu", "ip": "h"}, time.monotonic() + 5) is False


def test_cleanup_failure_refuses_and_names_it(monkeypatch):
    monkeypatch.setattr(acquisition, "_spawn_context", lambda: _NoReply(result=True))
    monkeypatch.setattr(acquisition, "_cleanup_owned_process", lambda p: False)
    with acquisition.capture_refusal_reason() as reasons:
        assert acquisition.bounded_status_is_idle({"api_type": "bambu", "ip": "h"}, time.monotonic() + 5) is False
    assert reasons == ["reader_cleanup_unconfirmed"]


def test_orchestration_message_carries_the_reason(monkeypatch):
    def not_idle(creds, deadline):
        acquisition._refusal_reasons.get().append("printer_not_idle")
        return False
    monkeypatch.setattr(acquisition, "bounded_status_is_idle", not_idle)
    reasons = []
    assert orchestration._readiness_confirmed({}, 0, reasons) is False and reasons == ["printer_not_idle"]
    monkeypatch.setattr(acquisition, "bounded_status_is_idle", lambda *a: False)  # doubles without reasons
    reasons = []
    assert orchestration._readiness_confirmed({}, 0, reasons) is False and reasons == []
