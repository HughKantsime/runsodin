"""Callback processing boundary and observation dedup, not notification delivery."""
import pytest
from tests.test_telemetry.test_legacy_monitor_capture import capture_store
from tests.test_telemetry.test_legacy_hms_capture import ERROR

@pytest.fixture
def processing(capture_store, monkeypatch):
    from modules.notifications import event_dispatcher
    calls=[]
    def process(printer_id, errors, observed_filename=None):
        calls.append((printer_id,errors,observed_filename))
    monkeypatch.setattr(event_dispatcher,'process_hms_errors',process)
    return capture_store,calls,event_dispatcher


def test_processing_same_error_after_explicit_clear(processing):
    (path,clock,send),calls,_=processing
    send(hms=ERROR)
    clock[0]=1001
    send(hms=[])
    clock[0]=1010
    send(hms=ERROR)
    assert len([c for c in calls if c[1]==ERROR])==2


def test_processing_same_error_on_new_filename(processing):
    (path,clock,send),calls,_=processing
    send(hms=ERROR,filename='synthetic-first.3mf')
    clock[0]=1010
    send(hms=ERROR,filename='synthetic-second.3mf')
    assert [c[2] for c in calls]==['synthetic-first.3mf','synthetic-second.3mf']


def test_processing_raised_failure_does_not_suppress_identical_retry(processing,monkeypatch):
    (path,clock,send),calls,module=processing
    def fail_once(printer_id,errors,observed_filename=None):
        calls.append((printer_id,errors,observed_filename))
        if len(calls)==1:
            raise RuntimeError('Synthetic processing failure')
    monkeypatch.setattr(module,'process_hms_errors',fail_once)
    with pytest.raises(RuntimeError,match='Synthetic processing failure'):
        send(hms=ERROR)
    clock[0]=1010
    send(hms=ERROR)
    assert len(calls)==2


@pytest.mark.parametrize('missing',[True,False])
def test_processing_unchanged_error_missing_or_null_does_not_invent_clear(processing,missing):
    (path,clock,send),calls,_=processing
    send(hms=ERROR,filename='synthetic-first.3mf')
    clock[0]=1001
    if missing:
        send()
    else:
        send(hms=None)
    clock[0]=1010
    send(hms=ERROR,filename='synthetic-first.3mf')
    assert len(calls)==1
