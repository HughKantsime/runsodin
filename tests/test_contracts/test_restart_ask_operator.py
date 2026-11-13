"""Restart questions through real callbacks and authenticated HTTP; no devices."""
import json
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_economic_creator_wiring import creators


@pytest.fixture
def case(creators, review_db, monkeypatch):
    from core.db_utils import _ConnectionAdapter
    from modules.printers.monitors import mqtt_printer, mqtt_job_lifecycle
    from modules.printers.routes_dispatch_recovery import router
    from modules.notifications import event_dispatcher
    from modules.archives import archive
    client, auth, token, _ = creators
    client.app.include_router(router, prefix='/api')
    from core.registry import registry
    from modules.orders.production import OrderFulfillmentService
    monkeypatch.setitem(registry._providers, 'OrderFulfillmentProvider', OrderFulfillmentService())
    engine = review_db.get_bind()
    with engine.begin() as db:
        db.execute(text("INSERT INTO jobs(id,item_name,printer_id,charged_to_org_id,status,hold) VALUES(238,'Retained start',9,1,'scheduled',1)"))
        db.execute(text("INSERT INTO printer_dispatch_claims(printer_id,job_id,physical_key,configuration_key,payload_sha256,token,owner_org_id,state) VALUES(9,238,:p,:c,:h,:t,1,'uncertain')"), {'p':'a'*64,'c':'b'*64,'h':'c'*64,'t':'d'*32})
        db.execute(text("INSERT INTO print_jobs(id,printer_id,job_name,filename,status,started_at,progress_percent,current_layer,remaining_minutes) VALUES(241,9,'bracket','plate.gcode','running',:start,13,6,615)"), {'start': (datetime.now(timezone.utc)-timedelta(hours=2)).isoformat()})
        db.execute(text("UPDATE users SET role='operator' WHERE id=2"))
    @contextmanager
    def connect():
        raw = engine.raw_connection()
        try:
            yield _ConnectionAdapter(raw, False)
        finally:
            raw.close()
    monkeypatch.setattr(mqtt_printer, 'get_db', connect)
    monkeypatch.setattr(mqtt_job_lifecycle, 'get_db', connect)
    monkeypatch.setattr(archive, 'get_db', connect)
    monkeypatch.setattr(event_dispatcher, 'increment_care_counters', lambda *a: None)
    def monitor():
        result = mqtt_printer.PrinterMonitor(9, 'Test printer', '127.0.0.1', 'SYNTHETIC', 'synthetic')
        result._last_heartbeat = result._last_spool_check = time.time()
        result._camera_discovered = True
        result._dispatch_alert = lambda **kw: None
        result._trigger_reschedule = lambda: None
        result._status_received.set()
        return result
    def rows():
        with engine.connect() as db:
            return [dict(r) for r in db.execute(text('SELECT * FROM print_jobs ORDER BY id')).mappings()]
    def question():
        from core.restart_questions import load
        with engine.connect() as db:
            return load(db, 'restart_question:9')
    def post(answer, qid=None, user='admin', headers=None):
        return client.post('/api/printers/9/dispatch-recovery/restart-question', headers=headers or auth(user),
                           json={'question_id': qid or question()['id'], 'answer': answer})
    return SimpleNamespace(engine=engine, monitor=monitor, rows=rows, question=question, post=post,
                           client=client, auth=auth, token=token, connect=connect)


def packet(monitor, **fields):
    monitor._on_status(SimpleNamespace(raw_data={'print': fields}))


def start(case, name='bracket'):
    monitor = case.monitor()
    packet(monitor, gcode_state='RUNNING', **({'subtask_name': name} if name else {}))
    return monitor


@pytest.mark.parametrize('first', ['callback', 'recovery'])
def test_restart_exact_duplicate_path_leaves_row_unchanged_and_claim_retained(case, first):
    before = case.rows()
    monitor = case.monitor()
    if first == 'recovery':
        monitor._state = {'gcode_state':'RUNNING', 'subtask_name':'bracket'}
        monitor._recover_orphaned_jobs()
    packet(monitor, gcode_state='RUNNING', subtask_name='bracket', mc_percent=50, layer_num=40, mc_remaining_time=30)
    monitor._recover_orphaned_jobs()
    for _ in range(3):
        packet(monitor, mc_percent=80, layer_num=60)
    assert case.rows() == before
    assert monitor._current_job_id is None
    assert monitor._state['mc_percent'] == 80
    assert case.question()['state'] == 'pending'
    with case.engine.connect() as db:
        assert db.execute(text('SELECT state FROM printer_dispatch_claims')).scalar_one() == 'uncertain'
        assert set(db.execute(text('SELECT user_id FROM alerts')).scalars()) == {2, 3, 71}
        assert db.execute(text('SELECT count(*) FROM alerts')).scalar_one() == 3
        events = db.execute(text("SELECT data FROM ws_events WHERE event_type='alert_new'")).scalars().all()
        assert len(events) == 1
        assert set(json.loads(events[0])['_audience']['user_ids']) == {2,3,71}
        from core.dispatch_recovery import telemetry
        with Session(case.engine) as session:
            assert telemetry(session, {'printer': {'id':9,'last_seen':datetime.now(timezone.utc)-timedelta(seconds=1),'gcode_state':'RUNNING'}, 'target_jobs':[], 'observations':case.rows()}) == 'active_observation'


def test_nameless_named_finish_and_second_restart_one_question(case):
    monitor = start(case, None)
    original = case.question()
    packet(monitor, subtask_name='bracket', mc_percent=25)
    assert monitor._pending_running_state is None
    monitor = case.monitor()
    monitor._monitor_started_at = datetime.now(timezone.utc)
    packet(monitor, gcode_state='RUNNING', subtask_name='bracket')
    assert case.question()['id'] == original['id']
    packet(monitor, gcode_state='FINISH', mc_percent=100, layer_num=93)
    packet(monitor, gcode_state='FINISH')
    assert len(case.rows()) == 1
    row = case.rows()[0]
    assert row['status'] == 'completed' and row['current_layer'] == 93
    assert row['print_duration_seconds'] is None and row['filament_used_g'] is None
    assert case.question()['state'] == 'ended_unconfirmed'
    with case.engine.connect() as db:
        assert db.execute(text('SELECT count(*) FROM alerts')).scalar_one() == 3
        assert db.execute(text('SELECT count(*) FROM alerts WHERE is_dismissed IS FALSE')).scalar_one() == 0
        assert db.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one() == 1
        from core.restart_questions import load
        assert load(db,'restart_print:241')['usage_verified'] is False
    assert case.post('same', original['id']).status_code == 409


@pytest.mark.parametrize('decision', ['same', 'new'])
def test_answers_atomic_rows_audit_alerts_and_replay(case, decision):
    monitor = start(case)
    original = case.question()
    before = case.rows()[0]
    response = case.post(decision)
    assert response.status_code == 200, response.text
    assert case.post(decision, original['id']).status_code == 409
    rows = case.rows()
    if decision == 'same':
        assert rows == [before]
        identifier = 241
    else:
        assert len(rows) == 2
        assert rows[0]['status'] == 'cancelled' and rows[0]['ended_at'] is None
        assert rows[0]['current_layer'] == 6 and rows[0]['progress_percent'] == 13
        identifier = rows[1]['id']
        assert rows[1]['scheduled_job_id'] is None
        assert rows[1]['status'] == 'running'
        assert 'first-seen-mid-print' in json.loads(rows[1]['spool_snapshot'])['source']
    packet(monitor, mc_percent=75, layer_num=50)
    assert monitor._current_job_id == identifier
    assert case.rows()[-1]['progress_percent'] == 75
    # The decision survives a monitor object replacement within this process.
    monitor = start(case)
    assert monitor._current_job_id == identifier
    with case.engine.connect() as db:
        audits = db.execute(text("SELECT details FROM audit_logs WHERE action='restart_question_answered'")).scalars().all()
        assert len(audits) == 1
        details = json.loads(audits[0]) if isinstance(audits[0], str) else audits[0]
        assert details['answer'] == decision and details['actor_id'] == 3
        assert db.execute(text('SELECT count(*) FROM alerts WHERE is_dismissed IS FALSE')).scalar_one() == 0
        assert db.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one() == 1


@pytest.mark.parametrize('user', ['student', 'manager', 'foreign'])
def test_answer_rbac_and_tenant(case, user):
    start(case)
    before = case.rows()
    response = case.post('same', user=user)
    assert response.status_code in (403, 404)
    assert case.rows() == before and case.question()['state'] == 'pending'


def test_read_token_cannot_answer(case):
    start(case)
    assert case.post('same', headers=case.token(['read:printers'])).status_code == 403


def test_question_is_visible_in_recovery_and_hidden_from_foreign_tenant(case):
    start(case)
    response = case.client.get('/api/printers/9/dispatch-recovery', headers=case.auth())
    assert response.status_code == 200
    assert response.json()['restart_question']['print_job_id'] == 241
    assert response.json()['answer_restart_question'] is True
    assert case.client.get('/api/printers/9/dispatch-recovery', headers=case.auth('foreign')).status_code == 404


def test_without_claim_original_adoption(case):
    with case.engine.begin() as db:
        db.execute(text('DELETE FROM printer_dispatch_claims'))
    monitor = start(case)
    assert monitor._current_job_id == 241 and case.question() is None
    assert len(case.rows()) == 1


def test_different_name_original_new_row(case):
    monitor = start(case, 'another-bracket')
    assert case.question() is None
    assert len(case.rows()) == 2 and monitor._current_job_id != 241


def test_row_started_after_monitor_is_not_restart_question(case):
    monitor = case.monitor()
    monitor._monitor_started_at = datetime.now(timezone.utc)-timedelta(days=1)
    packet(monitor, gcode_state='RUNNING', subtask_name='bracket')
    assert case.question() is None and len(case.rows()) == 2


@pytest.mark.parametrize('state,error,outcome', [('FINISH',0,'completed'),('FAILED',7,'failed'),('FAILED',0x0300400C,'cancelled'),('IDLE',0,'cancelled')])
def test_unanswered_terminal_result(case, state, error, outcome):
    monitor = start(case)
    packet(monitor, gcode_state=state, print_error=error)
    assert case.rows()[0]['status'] == outcome
    assert case.question()['state'] == 'ended_unconfirmed'
    assert len(case.rows()) == 1


def test_archive_cannot_invent_unconfirmed_usage_or_duration(case):
    from modules.archives.archive import create_print_archive
    monitor = start(case)
    packet(monitor, gcode_state='FINISH')
    identifier = create_print_archive(241,9,True)
    assert identifier is not None
    with case.engine.connect() as db:
        assert db.execute(text('SELECT actual_duration_seconds,filament_used_grams,accounting_state,accounting_reason FROM print_archives WHERE id=:id'), {'id':identifier}).one() == (None,None,'unresolved','ended_while_unconfirmed')


def test_alert_write_failure_rolls_back_and_does_not_fall_through(case, monkeypatch):
    from modules.notifications import restart_alerts
    def fail(*a):
        raise RuntimeError('synthetic failure')
    monkeypatch.setattr(restart_alerts, 'stage', fail)
    before = case.rows()
    start(case)
    assert case.rows() == before and case.question() is None


@pytest.mark.parametrize('decision', ['same', 'new'])
def test_answer_then_end_records_only_the_chosen_row_and_keeps_claim(case, decision):
    monitor = start(case)
    assert case.post(decision).status_code == 200
    packet(monitor, gcode_state='FINISH', mc_percent=100)
    packet(monitor, gcode_state='FINISH')
    rows = case.rows()
    assert len(rows) == (1 if decision == 'same' else 2)
    assert rows[-1]['status'] == 'completed' and rows[-1]['ended_at']
    if decision == 'new':
        assert rows[0]['status'] == 'cancelled' and rows[0]['ended_at'] is None
    with case.engine.connect() as db:
        assert db.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one() == 1
        assert db.execute(text("SELECT status FROM jobs WHERE id=238")).scalar_one() == 'scheduled'


@pytest.mark.parametrize('decision', ['same', 'new'])
def test_audit_failure_rolls_back_answer_row_changes_and_alert_resolution(case, monkeypatch, decision):
    from core import audit
    start(case)
    before = case.rows()
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic audit failure')
    monkeypatch.setattr(audit, 'log_audit', fail)
    assert case.post(decision).status_code == 409
    assert case.rows() == before and case.question()['state'] == 'pending'
    with case.engine.connect() as db:
        assert db.execute(text('SELECT count(*) FROM alerts WHERE is_dismissed IS FALSE')).scalar_one() == 3


def test_retained_end_cannot_close_question(case):
    monitor = start(case)
    before = case.rows()
    monitor._on_status(SimpleNamespace(raw_data={'print': {'gcode_state':'FINISH'}}, mqtt_retained=True))
    assert case.rows() == before and case.question()['state'] == 'pending'
    packet(monitor, subtask_name='bracket')
    assert case.rows() == before
    packet(monitor, gcode_state='FINISH')
    assert case.question()['state'] == 'ended_unconfirmed'


def test_hms_cannot_write_pending_row(case, monkeypatch):
    from modules.notifications import event_dispatcher
    def fail(*args, **kwargs):
        pytest.fail('pending question entered HMS terminal writer')
    monitor = start(case)
    monkeypatch.setattr(event_dispatcher, 'process_hms_errors', fail)
    before = case.rows()
    packet(monitor, gcode_state='PAUSE', hms=[{'attr':1,'code':7}], mc_percent=80)
    assert case.rows() == before


def test_live_telemetry_keeps_publishing_while_pending(case, monkeypatch):
    from modules.printers.monitors import mqtt_printer
    from modules.printers.monitors import live_state
    emitted = []
    monkeypatch.setattr(mqtt_printer, 'ws_push', lambda kind, payload: emitted.append((kind, payload)))
    monkeypatch.setattr(mqtt_printer, 'mqtt_republish', None)
    monkeypatch.setattr(live_state, 'observe_mqtt', lambda *args: None)
    monitor = start(case)
    before = case.rows()
    monitor._last_heartbeat = 0
    packet(monitor, mc_percent=47, layer_num=42, nozzle_temper=210)
    assert case.rows() == before
    telemetry = [payload for kind, payload in emitted if kind == 'printer_telemetry']
    assert telemetry[-1]['progress'] == 47 and telemetry[-1]['current_layer'] == 42


def test_changed_authority_refuses_answer_and_preserves_rows(case):
    start(case)
    with case.engine.begin() as db:
        db.execute(text('UPDATE print_jobs SET scheduled_job_id=238 WHERE id=241'))
    before = case.rows()
    assert case.post('new').status_code in (404,409)
    assert case.rows() == before and case.question()['state'] == 'pending'


@pytest.mark.parametrize('decision', ['same', 'new'])
def test_answer_never_authorizes_adoption_after_a_later_process_restart(case, decision):
    start(case)
    previous = case.question()['id']
    assert case.post(decision).status_code == 200
    monitor = case.monitor()
    monitor._monitor_started_at = datetime.now(timezone.utc)
    before = case.rows()
    packet(monitor, gcode_state='RUNNING', subtask_name='bracket')
    assert monitor._current_job_id is None
    assert case.rows() == before
    assert case.question()['state'] == 'pending' and case.question()['id'] != previous


def test_concurrent_monitors_create_one_question_and_one_alert_per_recipient(case):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: start(case), range(2)))
    assert len(case.rows()) == 1 and case.question()['state'] == 'pending'
    with case.engine.connect() as db:
        assert db.execute(text('SELECT count(*) FROM alerts')).scalar_one() == 3


def test_concurrent_answers_have_one_winner(case):
    from concurrent.futures import ThreadPoolExecutor
    start(case)
    identifier = case.question()['id']
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda decision: case.post(decision, identifier).status_code, ['same','new']))
    assert sorted(statuses) == [200,409]
    with case.engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='restart_question_answered'")).scalar_one() == 1
        assert db.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one() == 1


@pytest.mark.parametrize('decision', ['same','new'])
def test_answer_racing_terminal_keeps_one_authoritative_outcome(case, decision):
    from concurrent.futures import ThreadPoolExecutor
    monitor = start(case)
    identifier = case.question()['id']
    with ThreadPoolExecutor(max_workers=2) as pool:
        answer = pool.submit(case.post, decision, identifier)
        terminal = pool.submit(packet, monitor, gcode_state='FINISH', mc_percent=100)
        response = answer.result()
        terminal.result()
    packet(monitor, gcode_state='FINISH')
    rows = case.rows()
    assert response.status_code in (200,409)
    if response.status_code == 409:
        assert len(rows) == 1 and case.question()['state'] == 'ended_unconfirmed'
    else:
        assert len(rows) == (2 if decision == 'new' else 1)
    assert rows[-1]['status'] == 'completed'
    with case.engine.connect() as db:
        assert db.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one() == 1


def test_nameless_report_with_generic_plate_path_still_asks(case):
    monitor = case.monitor()
    packet(monitor, gcode_state='RUNNING', gcode_file='Metadata/plate_1.gcode')
    assert len(case.rows()) == 1 and case.question()['state'] == 'pending'
    packet(monitor, subtask_name='bracket')
    packet(monitor, gcode_state='FINISH')
    assert len(case.rows()) == 1 and case.question()['state'] == 'ended_unconfirmed'


def test_answer_does_not_adopt_a_later_differently_named_print(case):
    monitor = start(case)
    assert case.post('same').status_code == 200
    packet(monitor, gcode_state='RUNNING', subtask_name='different-print')
    assert len(case.rows()) == 2
    assert monitor._current_job_id != 241
    assert case.rows()[0]['progress_percent'] == 13
    assert case.question()['state'] == 'superseded'
