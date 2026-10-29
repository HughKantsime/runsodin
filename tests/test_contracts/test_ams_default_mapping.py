"""Default AMS and file retry contracts through real dispatch/schedule boundaries."""
import hashlib
import json
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from tests.test_contracts.dispatch_test_support import configure_dispatch
from tests.test_contracts.test_ams_evidence_transactions import RUNTIME
from tests.test_contracts.test_ams_source_inspection import archive
from tests.test_contracts.test_economic_creator_wiring import creators as creators
from tests.test_contracts.test_education_review_workflow import review_db as review_db


def setup_bambu(review_db, tmp_path, monkeypatch, *, source=0, verified=True, model='X1C', slots=4):
    engine, path = configure_dispatch(review_db, tmp_path, monkeypatch, protocol='bambu', slot_count=slots)
    payload = archive(
        xml=f'<config><plate><metadata key="index" value="1"/><filament id="{source + 1}" type="PETG"/></plate></config>',
        settings=json.dumps({'filament_type': ['PETG'] * (source + 1)}),
        code=f'; filament_type = {";".join(["PETG"] * (source + 1))}\nM620 S{source}A\nM621 S{source}A\n'
        + ('T9\n' if not verified else ''))
    path.write_bytes(payload)
    with engine.begin() as conn:
        conn.execute(text('UPDATE printers SET machine_type=:model,model=:model,slot_count=:slots WHERE id=59'),
                     {'model': model, 'slots': slots})
        conn.execute(text('UPDATE jobs SET payload_sha256=:sha WHERE id IN (53,54)'),
                     {'sha': hashlib.sha256(payload).hexdigest()})
        conn.execute(text('UPDATE print_files SET filaments_json=:filaments WHERE id=52'),
                     {'filaments': json.dumps([{'slot': source + 1, 'type': 'PETG', 'color': '#112233'}])})
        conn.execute(text("UPDATE filament_slots SET slot_number=:slot,filament_type='PETG',color_hex='112233' WHERE printer_id=59"),
                     {'slot': source + 1})
    return engine, path


@contextmanager
def progress_record(engine):
    from core.durable_config import insert
    from modules.printers import dispatch_background as background
    token = 'a' * 32
    marker = background._context.set({'token': token, 'staged': Event()})
    with engine.begin() as conn:
        insert(conn, background.progress_key(token), {'job_id': 53, 'printer_id': 59,
               'org_id': 1, 'attempt_id': token, 'phase': 'preparing'})
    try:
        yield background
    finally:
        background._context.reset(marker)


@pytest.mark.parametrize('archived', [True, False])
@pytest.mark.parametrize('model', ['X1C', 'P1S', 'A1', 'H2D'])
@pytest.mark.parametrize('source', [0, 1])
@pytest.mark.parametrize('protocol', ['legacy', 'v2'])
def test_bambu_ams_verified_default_sends_derived_identity(review_db, tmp_path, monkeypatch, model, source, protocol, archived):
    from modules.printers import dispatch, dispatch_ams_gate as gate
    from modules.printers.telemetry import feature_flag
    from modules.printers.telemetry.bambu.ams_acquisition_collector import RawAMSSnapshot
    from modules.printers.telemetry.bambu.ams_evidence import RawTrayFact
    from modules.printers.telemetry.bambu.trusted_ams_acquisition import RuntimeBoundAMSResult
    engine, path = setup_bambu(review_db, tmp_path, monkeypatch, source=source, model=model)
    if not archived:
        with engine.begin() as conn:
            conn.execute(text('UPDATE jobs SET print_file_id=NULL,payload_sha256=NULL,plate_index=NULL WHERE id=53'))
            conn.execute(text('UPDATE models SET print_file_id=52 WHERE id=51'))
    monkeypatch.setattr(gate, 'read_runtime_identity', lambda: RUNTIME)
    monkeypatch.setattr(gate, 'revalidate_runtime_identity', lambda runtime: None)
    monkeypatch.setattr(feature_flag, 'is_v2_enabled', lambda: protocol == 'v2')
    monkeypatch.setattr(dispatch.time, 'sleep', lambda *args: None)
    events = []

    def acquired(config, **options):
        events.append('fresh_read')
        assert options['selected_slots'] == (source + 1,)
        snapshot = RawAMSSnapshot(options['operation_id'], 'c' * 32, 1, time.monotonic(),
            options['configuration_key'], 1, (RawTrayFact(source + 1, 0, source, 'PETG', 'reported'),), True)
        return RuntimeBoundAMSResult(True, snapshot, RUNTIME.incarnation, RUNTIME.clock_domain, True, None)

    monkeypatch.setattr(gate, 'read_current_selected_ams', acquired)

    def uploaded(staged, remote):
        events.append('upload')
        assert Path(staged).read_bytes() == path.read_bytes()
        return True

    def started(*args, **options):
        events.append('start')
        assert options == {'plate_num': 1, 'ams_mapping': [-1] * source + [source]}
        return True

    if protocol == 'legacy':
        from modules.printers.adapters import bambu
        monkeypatch.setattr(bambu, 'BambuPrinter', lambda **kwargs: SimpleNamespace(
            upload_file=uploaded, connect=lambda: True, start_print=started, disconnect=lambda: None))
    else:
        from modules.printers.telemetry.bambu import ftp_upload, session
        monkeypatch.setattr(ftp_upload, 'upload_file', lambda host, code, staged, remote: uploaded(staged, remote))
        monkeypatch.setattr(session, 'BambuCommandAdapter', lambda config: SimpleNamespace(
            start=lambda: None, is_connected=lambda: True, start_print=started, stop=lambda: None))
    with progress_record(engine) as background:
        result = dispatch.dispatch_job(59, 53)
        assert result[0], result
        background.progress('start_accepted', message=result[1])
    assert events == ['upload', 'fresh_read', 'start']
    with engine.connect() as conn:
        assert background.load(conn, background.result_key(53))['ams_mapping_derived'] is True
        assert conn.execute(text('SELECT ams_mapping FROM jobs WHERE id=53')).scalar_one() is None


@pytest.mark.parametrize('unreadable', [False, True])
def test_x1c_unverified_default_refuses_before_upload_with_exact_suggestion(review_db, tmp_path, monkeypatch, caplog, unreadable):
    from modules.printers import dispatch
    engine, path = setup_bambu(review_db, tmp_path, monkeypatch, verified=False)
    if unreadable:
        path.write_bytes(b'not a sliced archive')
        with engine.begin() as conn:
            conn.execute(text('UPDATE jobs SET payload_sha256=:sha WHERE id=53'),
                         {'sha': hashlib.sha256(path.read_bytes()).hexdigest()})
    monkeypatch.setattr(dispatch, '_dispatch_bambu', lambda *a, **kw: pytest.fail('upload/start reached'))
    events = []
    monkeypatch.setattr(dispatch, '_ws', lambda *args: events.append(args))
    refusals = []
    monkeypatch.setattr(dispatch, '_notify_start_outcome', lambda *a, **kw: refusals.append(kw['gate_refusal']))
    with progress_record(engine) as background:
        ok, message = dispatch.dispatch_job(59, 53)
        assert not ok
        background.progress('refused', message=message)
    assert 'No AMS mapping: choose the tray for each filament' in message
    assert 'AMS preview suggestion: 1→1 (exact)' in message
    assert 'ams_mapping_required' in message
    assert events == [(53, 'failed', message)]
    assert [r.code for r in refusals] == ['ams_mapping_required']
    assert any('gate_refusal_code=ams_mapping_required' in r.message for r in caplog.records)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one() == 0
        assert conn.execute(text('SELECT ams_mapping FROM jobs WHERE id=53')).scalar_one() is None
        assert background.load(conn, background.result_key(53))['message'] == message


@pytest.mark.parametrize('verified', [False, True])
def test_external_spool_default_unchanged(review_db, tmp_path, monkeypatch, verified):
    from modules.printers import dispatch
    setup_bambu(review_db, tmp_path, monkeypatch, verified=verified, slots=1)
    calls = []

    def transport(*args, **options):
        assert 'ams_mapping' not in options
        calls.append(True)
        return True, 'Print started'

    monkeypatch.setattr(dispatch, '_dispatch_bambu', transport)
    assert dispatch.dispatch_job(59, 53)[0]
    assert calls == [True]


def schedule_case(creators, review_db, tmp_path, monkeypatch, status, *, pointer=True):
    from modules.models_library.routes.print_files import router
    engine, _ = setup_bambu(review_db, tmp_path, monkeypatch)
    with engine.begin() as conn:
        conn.execute(text('UPDATE jobs SET status=:status WHERE id IN (53,54)'), {'status': status})
        if pointer:
            conn.execute(text('UPDATE print_files SET job_id=53 WHERE id=52'))
    client, auth, _, _ = creators
    client.app.include_router(router, prefix='/api')
    return client, auth


@pytest.mark.parametrize('status', ['completed', 'failed', 'cancelled'])
def test_terminal_file_can_be_scheduled_again(creators, review_db, tmp_path, monkeypatch, status):
    from modules.models_library.file_management import list_generic_files
    client, auth = schedule_case(creators, review_db, tmp_path, monkeypatch, status)
    available = list_generic_files(review_db, {'id': 3, 'role': 'admin', 'group_id': 1}, 100, False)
    assert 52 in [f['id'] for f in available]
    review_db.commit()
    response = client.post('/api/print-files/52/schedule?printer_id=59', headers=auth())
    assert response.status_code == 200, response.text
    job_id = response.json()['job_id']
    assert job_id not in (53, 54)
    review_db.expire_all()
    assert review_db.execute(text('SELECT job_id FROM print_files WHERE id=52')).scalar_one() == job_id
    assert review_db.execute(text('SELECT status FROM jobs WHERE id=53')).scalar_one() == status
    review_db.commit()
    assert client.post('/api/print-files/52/schedule?printer_id=59', headers=auth()).status_code == 409


@pytest.mark.parametrize('status', ['pending', 'submitted', 'scheduled', 'printing', 'paused'])
@pytest.mark.parametrize('pointer', [False, True])
def test_nonterminal_file_still_refuses_duplicate_schedule(creators, review_db, tmp_path, monkeypatch, status, pointer):
    client, auth = schedule_case(creators, review_db, tmp_path, monkeypatch, status, pointer=pointer)
    before = review_db.execute(text('SELECT count(*) FROM jobs')).scalar_one()
    review_db.commit()
    response = client.post('/api/print-files/52/schedule?printer_id=59', headers=auth())
    assert response.status_code == 409, response.text
    assert 'File already scheduled' in response.text
    assert review_db.execute(text('SELECT count(*) FROM jobs')).scalar_one() == before


def test_concurrent_terminal_file_retries_create_only_one_job(creators, review_db, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    client, auth = schedule_case(creators, review_db, tmp_path, monkeypatch, 'failed')
    headers = auth()
    barrier = Barrier(2)

    def schedule():
        barrier.wait(timeout=10)
        return client.post('/api/print-files/52/schedule?printer_id=59', headers=headers)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: schedule(), range(2)))
    assert sorted(r.status_code for r in responses) == [200, 409], [r.text for r in responses]
    assert review_db.execute(text("SELECT count(*) FROM jobs WHERE print_file_id=52 AND status='pending'")).scalar_one() == 1
