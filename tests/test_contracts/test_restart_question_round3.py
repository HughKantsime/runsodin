"""Review 3: print confirmation uses printer/row access, not claim recovery."""
import pytest
from sqlalchemy import text

from tests.test_contracts.test_restart_ask_operator import case, packet, start
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_economic_creator_wiring import creators


@pytest.fixture
def global_case(case):
    with case.engine.begin() as db:
        db.execute(text('DELETE FROM education_cost_center_printers WHERE printer_id=9'))
        db.execute(text('UPDATE printers SET org_id=NULL WHERE id=9'))
        db.execute(text('UPDATE printer_dispatch_claims SET owner_org_id=NULL WHERE printer_id=9'))
        db.execute(text('UPDATE jobs SET charged_to_org_id=NULL WHERE id=238'))
    return case


@pytest.mark.parametrize('user', ['unassigned', 'manager', 'admin'])
@pytest.mark.parametrize('decision', ['same', 'new'])
def test_global_printer_question_visible_and_answerable(global_case, user, decision):
    case = global_case
    start(case)
    before = case.rows()
    inspection = case.client.get('/api/printers/9/dispatch-recovery', headers=case.auth(user))
    assert inspection.status_code == 200
    data = inspection.json()
    assert data['restart_question']['id'] == case.question()['id']
    assert data['answer_restart_question'] is True
    assert data['actions'] == {'hold': False, 'reconcile_retain': False, 'release': False}
    assert 'attempt_handle' not in data
    response = case.post(decision, user=user)
    assert response.status_code == 200, response.text
    assert case.question()['state'] == decision
    if decision == 'same':
        assert case.rows() == before
    else:
        assert len(case.rows()) == 2
        assert case.rows()[0]['status'] == 'cancelled'
        assert case.rows()[1]['status'] == 'running'
    with case.engine.connect() as db:
        assert db.execute(text('SELECT state FROM printer_dispatch_claims')).scalar_one() == 'uncertain'
        assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='restart_question_answered'")).scalar_one() == 1
        assert db.execute(text('SELECT count(*) FROM alerts WHERE is_dismissed IS FALSE')).scalar_one() == 0


@pytest.mark.parametrize('principal', ['viewer', 'read_token'])
def test_global_question_readers_cannot_answer(global_case, principal):
    case = global_case
    start(case)
    headers = case.auth('student') if principal == 'viewer' else case.token(['read:printers'])
    data = case.client.get('/api/printers/9/dispatch-recovery', headers=headers).json()
    assert data['restart_question']['id'] == case.question()['id']
    assert data['answer_restart_question'] is False
    before = case.rows()
    assert case.post('same', headers=headers).status_code == 403
    assert case.rows() == before and case.question()['state'] == 'pending'


@pytest.mark.parametrize('user', ['unassigned', 'manager'])
@pytest.mark.parametrize('action', ['hold', 'reconcile-retain', 'release'])
def test_global_operator_has_no_claim_recovery_powers(global_case, user, action):
    start(global_case)
    response = global_case.client.post(f'/api/printers/9/dispatch-recovery/{action}',
                                       headers=global_case.auth(user), json={})
    assert response.status_code == 403


def test_operator_cannot_read_or_answer_another_orgs_question(case):
    with case.engine.begin() as db:
        db.execute(text('DELETE FROM education_cost_center_printers WHERE printer_id=9'))
        db.execute(text('UPDATE printers SET org_id=2 WHERE id=9'))
        db.execute(text('UPDATE printer_dispatch_claims SET owner_org_id=2 WHERE printer_id=9'))
        db.execute(text('UPDATE jobs SET charged_to_org_id=2 WHERE id=238'))
    start(case)
    assert case.client.get('/api/printers/9/dispatch-recovery', headers=case.auth('manager')).status_code == 404
    assert case.post('same', user='manager').status_code == 404


@pytest.mark.parametrize('kind', ['printer', 'print_row'])
@pytest.mark.parametrize('decision', ['same', 'new'])
def test_global_question_keeps_controlled_part_checks(global_case, kind, decision):
    case = global_case
    with case.engine.begin() as db:
        db.execute(text("INSERT INTO jobs(id,item_name,printer_id,status) VALUES(239,'Earlier',9,'printing')"))
        db.execute(text('UPDATE print_jobs SET scheduled_job_id=239 WHERE id=241'))
    start(case)
    with case.engine.begin() as db:
        db.execute(text("INSERT INTO controlled_audiences(id,name) VALUES(91,'Restricted')"))
        db.execute(text("INSERT INTO controlled_jobs(job_id,label,audience_id,printer_tag,retention_days,reason,marked_at) "
                        "VALUES(:job,'Restricted',91,'Secure',7,'Test restriction','2026-11-13T12:00:00Z')"),
                   {'job': 238 if kind == 'printer' else 239})
    before = case.rows()
    data = case.client.get('/api/printers/9/dispatch-recovery', headers=case.auth('manager')).json()
    assert data['restart_question'] is None and data['answer_restart_question'] is False
    assert case.post(decision, user='manager').status_code == 404
    assert case.rows() == before and case.question()['state'] == 'pending'


@pytest.mark.parametrize('decision', ['same', 'new'])
def test_question_cannot_take_over_an_education_row(case, decision):
    start(case)
    with case.engine.begin() as db:
        db.execute(text('UPDATE jobs SET printer_id=9 WHERE id=13'))
        db.execute(text('UPDATE print_jobs SET scheduled_job_id=13 WHERE id=241'))
    before = case.rows()
    data = case.client.get('/api/printers/9/dispatch-recovery', headers=case.auth('manager')).json()
    assert data['restart_question'] is None and data['answer_restart_question'] is False
    assert case.post(decision, user='manager').status_code == 404
    assert case.rows() == before


def test_idle_investigation_retires_after_admin_closes_rows(case):
    with case.engine.begin() as db:
        db.execute(text("INSERT INTO print_jobs(id,printer_id,job_name,status,started_at) SELECT 242,9,job_name,status,started_at FROM print_jobs WHERE id=241"))
    monitor = start(case)
    packet(monitor, gcode_state='IDLE')
    original = case.question()
    assert original['state'] == 'investigation'
    with case.engine.begin() as db:
        db.execute(text("UPDATE print_jobs SET status='cancelled' WHERE printer_id=9"))
    before = case.rows()
    packet(monitor, gcode_state='IDLE')
    assert case.question() is None
    assert case.rows() == before
    inspection = case.client.get('/api/printers/9/dispatch-recovery', headers=case.auth()).json()
    assert inspection['restart_question'] is None
    with case.engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM alerts WHERE title='Print records need investigation' AND is_dismissed IS FALSE")).scalar_one() == 0
        assert db.execute(text("SELECT count(*) FROM alerts WHERE title='Print confirmation closed'")).scalar_one() == 3
    for _ in range(3):
        packet(monitor, gcode_state='IDLE')
    with case.engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM alerts WHERE title='Print confirmation closed'")).scalar_one() == 3
