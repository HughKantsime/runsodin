"""Authenticated async dispatch uses real durable claims; only device I/O is replaced."""
import threading
import time
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db
from tests.test_contracts.dispatch_test_support import configure_dispatch


def test_202_handle_duplicate_and_status_progress(token_client, review_db, tmp_path, monkeypatch):
    from modules.printers import dispatch
    from modules.printers.dispatch_background import progress
    from modules.jobs.routes import jobs_lifecycle, dispatch_status
    engine, _ = configure_dispatch(review_db, tmp_path, monkeypatch)
    client, issue = token_client
    client.app.include_router(jobs_lifecycle.router)
    client.app.include_router(dispatch_status.router)
    headers = issue(['write:jobs', 'read:jobs'])
    release = threading.Event()
    entered = threading.Event()
    calls = []
    def adapter(*args):
        calls.append(args[0])
        progress('uploading')
        entered.set()
        assert release.wait(10)
        progress('starting')
        return True, 'Print started'
    monkeypatch.setattr(dispatch, '_dispatch_moonraker', adapter)
    try:
        response = client.post('/jobs/53/dispatch', headers=headers)
        assert response.status_code == 202, response.text
        body = response.json()
        assert len(body['attempt_id']) == 32
        assert body['status_url'] == '/api/jobs/53/dispatch-status?attempt_id=' + body['attempt_id']
        with engine.connect() as conn:
            assert conn.execute(text('SELECT token FROM printer_dispatch_claims WHERE job_id=53')).scalar_one() == body['attempt_id']
        assert entered.wait(5)
        url = body['status_url'].removeprefix('/api')
        assert client.get(url, headers=headers).json()['phase'] == 'uploading'
        assert client.post('/jobs/53/dispatch', headers=headers).status_code == 409
        assert calls == [53]
        assert client.get(url.replace('/53/', '/54/'), headers=headers).status_code == 404
    finally:
        release.set()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = client.get(url, headers=headers).json()
        if result['phase'] == 'start_accepted':break
        time.sleep(.02)
    assert result['phase'] == 'start_accepted'
    assert calls == [53]
    # Job and printer scope apply equally to token-qualified status reads.
    review_db.execute(text('UPDATE printers SET org_id=2,shared=FALSE WHERE id=59'));review_db.commit()
    assert client.get(url, headers=headers).status_code == 404


def test_sync_compatibility_path(token_client, review_db, tmp_path, monkeypatch):
    from modules.printers import dispatch
    from modules.jobs.routes import jobs_lifecycle
    configure_dispatch(review_db,tmp_path,monkeypatch)
    client, issue = token_client
    client.app.include_router(jobs_lifecycle.router)
    monkeypatch.setattr(dispatch,'_dispatch_moonraker',lambda *a:(True,'Print started'))
    result = client.post('/jobs/53/dispatch?wait=true',headers=issue(['write:jobs']))
    assert result.status_code == 200 and result.json()['success'] is True


def test_start_next_202_replay_and_worker_preserves_bed_confirmation(token_client, review_db, tmp_path, monkeypatch):
    import uuid
    from modules.printers import dispatch, start_next, routes_start_next
    from modules.jobs.routes import dispatch_status
    engine, _ = configure_dispatch(review_db,tmp_path,monkeypatch)
    with engine.begin() as conn:conn.execute(text("UPDATE jobs SET status='pending' WHERE id=53"))
    suggestion = {'job_id':53,'fingerprint':'f'*64}
    monkeypatch.setattr(start_next,'next_suggestion',lambda *a,**kw:suggestion)
    client, issue = token_client
    client.app.include_router(routes_start_next.router)
    client.app.include_router(dispatch_status.router)
    headers = issue(['write:jobs','read:jobs'])
    entered, release = threading.Event(), threading.Event()
    calls = []
    def adapter(*args):
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM job_timeline_events WHERE job_id=53 AND kind='bed_clear_confirmed'")).scalar_one() == 1
        calls.append(True);entered.set()
        assert release.wait(10)
        return True,'Print started'
    monkeypatch.setattr(dispatch,'_dispatch_moonraker',adapter)
    body = {'suggested_job_id':53,'suggestion_fingerprint':'f'*64,'request_id':str(uuid.uuid4()),
            'bed_clear_confirmed':True,'attestation':'owner_in_app'}
    try:
        response = client.post('/printers/59/start-next',headers=headers,json=body)
        assert response.status_code == 202, response.text
        handle = response.json()
        assert handle['attempt_id'] and handle['accepted'] is True
        assert entered.wait(5)
        replay = client.post('/printers/59/start-next',headers=headers,json=body)
        assert replay.status_code == 202 and replay.json()['attempt_id'] == handle['attempt_id']
        assert calls == [True]
    finally:release.set()
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        result=client.get(handle['status_url'].removeprefix('/api'),headers=headers).json()
        if result['phase']=='start_accepted':break
        time.sleep(.02)
    assert result['phase']=='start_accepted'
    replay = client.post('/printers/59/start-next?wait=true',headers=headers,json=body)
    assert replay.status_code == 200 and replay.json()['success'] is True
    assert calls == [True]
