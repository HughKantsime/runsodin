"""Review 2 repros: real packets and HTTP against synthetic SQLite only."""
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from sqlalchemy import text, event
from tests.test_contracts.test_restart_ask_operator import case, start, packet
from tests.test_contracts.test_restart_question_review import trace_connections
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_economic_creator_wiring import creators


@pytest.mark.parametrize('change', ['link', 'claim_link', 'unlink', 'token', 'started_at', 'reassigned', 'close_one'])
def test_retirement_rechecks_before_any_insert(case, change):
    with case.engine.begin() as db:
        db.execute(text("INSERT INTO jobs(id,item_name,printer_id,charged_to_org_id,status) VALUES(239,'Earlier',9,1,'printing')"))
        if change in ('unlink', 'reassigned'):
            db.execute(text('UPDATE print_jobs SET scheduled_job_id=239 WHERE id=241'))
        if change == 'close_one':
            db.execute(text("INSERT INTO print_jobs(id,printer_id,job_name,status,started_at) SELECT 242,9,job_name,status,started_at FROM print_jobs WHERE id=241"))
    monitor = start(case)
    old = case.question()['id']
    with case.engine.begin() as db:
        db.execute(text({
            'link': 'UPDATE print_jobs SET scheduled_job_id=239 WHERE id=241',
            'claim_link': 'UPDATE print_jobs SET scheduled_job_id=238 WHERE id=241',
            'unlink': 'UPDATE print_jobs SET scheduled_job_id=NULL WHERE id=241',
            'token': "UPDATE printer_dispatch_claims SET token='eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee'",
            'started_at': 'UPDATE print_jobs SET started_at=CURRENT_TIMESTAMP WHERE id=241',
            'reassigned': 'UPDATE jobs SET printer_id=NULL WHERE id=239',
            'close_one': "UPDATE print_jobs SET status='cancelled' WHERE id=242",
        }[change]))
    before = case.rows()
    for _ in range(3): packet(monitor, gcode_state='RUNNING', subtask_name='bracket', mc_percent=80)
    assert case.rows() == before
    assert monitor._current_job_id is None
    assert case.question()['id'] != old
    assert case.question()['state'] == ('investigation' if change in ('reassigned', 'claim_link') else 'pending')
    packet(monitor, gcode_state='FINISH', mc_percent=100)
    assert len(case.rows()) == len(before)
    if change not in ('reassigned', 'claim_link'):
        assert case.rows()[0]['status'] == 'completed'
        assert case.question() is None


@pytest.mark.parametrize('investigation', [False, True])
def test_pending_packets_have_one_read_and_no_state_effects(case, monkeypatch, investigation):
    from modules.printers.monitors import mqtt_printer
    from core import job_timeline
    if investigation:
        with case.engine.begin() as db:
            db.execute(text("INSERT INTO print_jobs(id,printer_id,job_name,status,started_at) SELECT 242,9,job_name,status,started_at FROM print_jobs WHERE id=241"))
    monitor = start(case)
    log, attention = Mock(), Mock()
    monkeypatch.setattr(mqtt_printer.log, 'info', log)
    monkeypatch.setattr(job_timeline, 'close_pause_episode', attention)
    calls = trace_connections(case, monkeypatch)
    before = case.rows()
    for percent in range(20, 30): packet(monitor, gcode_state='RUNNING', mc_percent=percent)
    assert not log.called and not attention.called
    sql = [s for s in calls if s != 'CONNECT']
    assert len(sql) == 10 and all(s.lstrip().upper().startswith('SELECT') for s in sql)
    assert case.rows() == before


@pytest.mark.parametrize('decision', ['same', 'new'])
def test_answer_before_restarted_monitor_first_packet_is_honoured(case, decision):
    start(case)
    monitor = case.monitor()
    monitor._monitor_started_at = datetime.now(timezone.utc)
    response = case.post(decision)
    assert response.status_code == 200, response.text
    chosen = response.json()['print_job_id']
    before = case.rows()
    packet(monitor, gcode_state='RUNNING', subtask_name='bracket')
    assert len(case.rows()) == len(before)
    assert monitor._current_job_id == chosen
    packet(monitor, gcode_state='FINISH')
    assert case.rows()[-1]['status'] == 'completed'
    assert case.question() is None


def test_investigation_visible_and_idle_stops_packet_work(case, monkeypatch):
    with case.engine.begin() as db:
        db.execute(text("INSERT INTO print_jobs(id,printer_id,job_name,status,started_at) SELECT 242,9,job_name,status,started_at FROM print_jobs WHERE id=241"))
    monitor = start(case)
    response = case.client.get('/api/printers/9/dispatch-recovery', headers=case.auth())
    question = response.json()['restart_question']
    assert question and question['candidate_ids'] == [241, 242]
    assert 'close' in question['message'].lower()
    packet(monitor, gcode_state='FINISH')
    packet(monitor, gcode_state='IDLE')
    before = case.rows()
    calls = trace_connections(case, monkeypatch)
    for _ in range(6): packet(monitor, gcode_state='IDLE')
    assert calls == []
    packet(monitor, gcode_state='RUNNING', subtask_name='other-print')
    assert len(case.rows()) == len(before) + 1


def test_first_idle_after_restart_records_unknown_not_cancellation(case):
    start(case)
    monitor = case.monitor()
    monitor._monitor_started_at = datetime.now(timezone.utc)
    packet(monitor, gcode_state='IDLE')
    row = case.rows()[0]
    assert 'unknown' in row['error_code'].lower()
    assert row['ended_at'] is None
    assert row['progress_percent'] == 13


def test_gcode_file_only_different_print_does_not_close_earlier_row(case):
    monitor = start(case, None)
    before = case.rows()[0]
    packet(monitor, gcode_state='FINISH', gcode_file='another.gcode', mc_percent=100)
    assert case.rows()[0] == before
    assert case.question() is None


def test_unanswered_end_notifies_once(case):
    monitor = start(case)
    for _ in range(3): packet(monitor, gcode_state='FINISH')
    with case.engine.connect() as db:
        notices = db.execute(text("SELECT message FROM alerts WHERE is_dismissed IS FALSE")).scalars().all()
    assert len(notices) == 3
    assert all('ended' in message.lower() and 'unanswered' in message.lower() for message in notices)


def test_ordinary_archive_has_no_restart_lookup(case, monkeypatch):
    from modules.archives import archive
    with case.engine.begin() as db:
        db.execute(text('DELETE FROM printer_dispatch_claims'))
    monitor = start(case)
    from core import restart_questions
    original = restart_questions.load
    looked_up = []
    def load(db, key):
        looked_up.append(key)
        return original(db, key)
    monkeypatch.setattr(restart_questions, 'load', load)
    calls = trace_connections(case, monkeypatch)
    from modules.printers.monitors import mqtt_printer
    monkeypatch.setattr(archive, 'get_db', mqtt_printer.get_db)
    packet(monitor, gcode_state='FINISH')
    assert 'restart_print:241' not in looked_up
    with case.engine.connect() as db:
        assert db.execute(text('SELECT count(*) FROM print_archives')).scalar_one() == 1


def test_print_jobs_marks_pending_and_has_no_per_row_config_reads(case):
    from modules.jobs.routes.presets import router
    case.client.app.include_router(router, prefix='/api')
    start(case)
    calls = []
    def capture(conn, cursor, statement, parameters, context, executemany): calls.append(statement)
    event.listen(case.engine, 'before_cursor_execute', capture)
    try:
        response = case.client.get('/api/print-jobs', headers=case.auth())
    finally:
        event.remove(case.engine, 'before_cursor_execute', capture)
    assert response.status_code == 200, response.text
    assert response.json()[0]['awaiting_confirmation'] is True
    assert not any('SELECT value FROM system_config WHERE key=' in s for s in calls)


@pytest.mark.parametrize('decision', ['same', 'new'])
def test_operator_can_answer_without_recovery_powers(case, decision):
    start(case)
    inspection = case.client.get('/api/printers/9/dispatch-recovery', headers=case.auth('manager')).json()
    assert inspection['answer_restart_question'] is True
    assert not inspection['actions']['reconcile_retain'] and not inspection['actions']['release']
    assert case.post(decision, user='manager').status_code == 200
    for action in ('reconcile-retain', 'release'):
        assert case.client.post(f'/api/printers/9/dispatch-recovery/{action}', headers=case.auth('manager'), json={}).status_code == 403

@pytest.mark.parametrize('decision', ['same', 'new'])
def test_consumed_answer_is_not_identity_evidence_after_another_restart(case, decision):
    monitor = start(case)
    assert case.post(decision).status_code == 200
    packet(monitor, gcode_state='RUNNING')  # decision consumed: recording resumed
    assert monitor._current_job_id is not None
    monitor = case.monitor()
    monitor._monitor_started_at = datetime.now(timezone.utc)
    before = case.rows()
    packet(monitor, gcode_state='RUNNING', subtask_name='bracket')
    assert monitor._current_job_id is None
    assert case.rows() == before
    assert case.question()['state'] == 'pending'
