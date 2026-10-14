"""Actual dispatch entry concurrency with owned file and substituted transports."""
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.dispatch_test_support import configure_dispatch


@pytest.mark.parametrize('same_job',[True,False],ids=['same-job','different-job-same-printer'])
def test_second_dispatch_cannot_enter_adapter_during_first_attempt(tmp_path,monkeypatch,same_job,review_db):
    from modules.printers import dispatch
    engine,file=configure_dispatch(review_db,tmp_path,monkeypatch)
    entered=threading.Event();release=threading.Event();calls=[];guard=threading.Lock()
    def adapter(*args,**kwargs):
        with guard:
            calls.append(args[0]);first=len(calls)==1
        if first:
            entered.set();assert release.wait(timeout=5)
        return False,'Synthetic upload refusal'
    monkeypatch.setattr(dispatch,'_dispatch_moonraker',adapter)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first=executor.submit(dispatch.dispatch_job,59,53)
        try:
            assert entered.wait(timeout=2)
            second=dispatch.dispatch_job(59,53 if same_job else 54)
            assert second[0] is False
            assert len(calls)==1,'Concurrent dispatch entered the upload-capable adapter twice'
        finally:
            release.set();assert first.result(timeout=3)[0] is False
