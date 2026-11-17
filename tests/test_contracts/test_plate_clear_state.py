"""The plate-clear read advertises the writer's selection without leaking job names."""
import pytest
from sqlalchemy import event, text
from tests.test_contracts.test_plate_clear_confirmation import floor, finished, print_record, archive_submission  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_api_token_delegation_behavior import token_client  # noqa: F401


@pytest.fixture
def plate_client(floor, monkeypatch):
    from modules.printers.routes_integrations import router
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    client, issue, db = floor
    # The shared Education fixture omits ORM defaults on its two seed printers.
    db.execute(text("UPDATE printers SET slot_count=COALESCE(slot_count,4),tags=COALESCE(tags,'[]'),"
                    "timelapse_enabled=COALESCE(timelapse_enabled,0),shared=COALESCE(shared,0)"))
    db.commit()
    client.app.include_router(router)
    return client, issue, db


def read(client, headers, surface, pid=199):
    response = client.get({'detail': f'/printers/{pid}', 'list': '/printers',
                           'feed': '/integrations/home-assistant/status'}[surface], headers=headers)
    assert response.status_code == 200, response.text
    data = response.json()
    return data if surface == 'detail' else next(p for p in (data['printers'] if surface == 'feed' else data) if p['id'] == pid)


@pytest.mark.parametrize('surface', ['detail', 'list', 'feed'])
@pytest.mark.parametrize('linked', [False, True])
def test_finished_print_needs_clearing_and_press_clears_state(plate_client, surface, linked):
    client, issue, db = plate_client
    if linked:
        finished(db, 802)
    else:
        print_record(db)
    headers = issue(['read', 'write:printers'])
    before = read(client, headers, surface)['plate_clear']
    assert before['needed'] is True and before['releases_job'] is None
    assert before['last_print_ended_at'] is not None and before['has_waiting_job'] is False
    reply = client.post('/printers/199/plate-cleared', headers=headers)
    assert reply.status_code == 200 and reply.json()['message'] == 'Plate clear recorded'
    after = read(client, headers, surface)
    assert after['plate_clear']['needed'] is False and after['plate_clear_confirmed_at']


@pytest.mark.parametrize('surface', ['detail', 'list', 'feed'])
def test_no_prints_do_not_need_clearing(plate_client, surface):
    client, issue, _ = plate_client
    assert read(client, issue(['read']), surface)['plate_clear'] == {
        'needed': False, 'has_waiting_job': False, 'releases_job': None, 'last_print_ended_at': None}


def waiting(db, identifier=803, org=1, priority=3, position=None, pid=199):
    db.execute(text("INSERT INTO jobs(id,item_name,printer_id,charged_to_org_id,status,priority,queue_position) "
                    "VALUES(:id,'Lid.gcode',:p,:org,'waiting_plate_clear',:priority,:position)"),
               {'id': identifier, 'p': pid, 'org': org, 'priority': priority, 'position': position})
    db.commit()


@pytest.mark.parametrize('surface', ['detail', 'list', 'feed'])
def test_waiting_job_uses_display_name_and_matches_writer(plate_client, surface):
    client, issue, db = plate_client
    waiting(db)
    headers = issue(['read', 'write:printers'])
    assert read(client, headers, surface)['plate_clear']['releases_job'] == {'id': 803, 'name': 'Lid'}
    assert client.post('/printers/199/plate-cleared', headers=headers).json()['message'] == 'Job 803 released for dispatch'
    assert read(client, headers, surface)['plate_clear']['needed'] is False


@pytest.mark.parametrize('hidden', ['organization', 'controlled', 'archived', 'scope'])
def test_hidden_first_job_is_never_replaced_with_a_visible_job(plate_client, hidden):
    from tests.test_contracts.test_home_assistant_status import _mark_controlled
    client, issue, db = plate_client
    waiting(db, org=2 if hidden == 'organization' else 1, priority=1)
    waiting(db, identifier=804, priority=4)
    if hidden == 'controlled':
        _mark_controlled(db, 803)
    if hidden == 'archived':
        archive_submission(db, 803)
    db.commit()
    headers = issue(['read:printers'] if hidden == 'scope' else ['read'])
    for surface in (['list', 'detail'] if hidden == 'scope' else ['list', 'detail', 'feed']):
        plate = read(client, headers, surface)['plate_clear']
        assert plate['needed'] is True and plate['has_waiting_job'] is True
        assert plate['releases_job'] is None


@pytest.mark.parametrize('state', ['RUNNING', 'PRINTING', 'PAUSE', 'PAUSED', 'job', 'monitor'])
def test_active_print_suppresses_control_even_with_waiting_job(plate_client, state):
    client, issue, db = plate_client
    finished(db, 802)
    waiting(db)
    if state == 'job':
        db.execute(text("UPDATE jobs SET status='paused' WHERE id=802"))
    elif state == 'monitor':
        print_record(db, status='running', end=None)
    else:
        db.execute(text('UPDATE printers SET gcode_state=:s WHERE id=199'), {'s': state})
    db.commit()
    for surface in ['list', 'detail', 'feed']:
        assert read(client, issue(['read']), surface)['plate_clear']['needed'] is False


def test_read_printers_scope_observes_confirmation_without_job_names(plate_client):
    client, issue, db = plate_client
    finished(db, 802)
    headers = issue(['read:printers', 'write:printers'])
    assert read(client, headers, 'detail')['plate_clear']['needed'] is True
    client.post('/printers/199/plate-cleared', headers=headers)
    assert read(client, headers, 'detail')['plate_clear']['needed'] is False


@pytest.mark.parametrize('surface', ['list', 'feed'])
@pytest.mark.parametrize('kind', ['finished', 'waiting'])
def test_fifty_printers_add_no_per_printer_queries(plate_client, surface, kind):
    client, issue, db = plate_client
    headers = issue(['read'])
    def seed(pid):
        db.execute(text("INSERT INTO printers(id,name,api_type,org_id,gcode_state,slot_count,is_active,tags,timelapse_enabled,shared) "
                        "VALUES(:id,:name,'moonraker',1,'FINISH',4,1,'[]',0,0)"), {'id': pid, 'name': f'Batch printer {pid}'})
        if kind == 'finished':
            db.execute(text("INSERT INTO print_jobs(printer_id,status,started_at,ended_at) "
                            "VALUES(:id,'completed','2020-01-01 10:00:00','2020-01-01 11:00:00')"), {'id': pid})
        else:
            waiting(db, identifier=pid + 10000, pid=pid)
        db.commit()
    def count():
        statements = []
        def capture(conn, cursor, statement, parameters, context, many):
            statements.append(statement)
        event.listen(db.get_bind(), 'before_cursor_execute', capture)
        try:
            read(client, headers, surface, pid=1000)
        finally:
            event.remove(db.get_bind(), 'before_cursor_execute', capture)
        return len(statements)
    seed(1000)
    single = count()
    for pid in range(1001, 1050):
        seed(pid)
    fifty = count()
    assert fifty <= single + 2, (single, fifty)


def test_queue_order_matches_writer_and_another_waiting_job_keeps_control_visible(plate_client):
    client, issue, db = plate_client
    waiting(db, identifier=803, position=None)
    waiting(db, identifier=804, position=1)
    headers = issue(['read', 'write:printers'])
    assert read(client, headers, 'detail')['plate_clear']['releases_job']['id'] == 804
    assert client.post('/printers/199/plate-cleared', headers=headers).json()['message'] == 'Job 804 released for dispatch'
    plate = read(client, headers, 'detail')['plate_clear']
    assert plate['needed'] is True and plate['releases_job']['id'] == 803


def test_a_later_print_invalidates_the_confirmation(plate_client, monkeypatch):
    from datetime import datetime, timezone
    from tests.test_contracts.test_plate_clear_confirmation import set_clock
    client, issue, db = plate_client
    print_record(db, start='2020-01-01 10:00:00', end='2020-01-01 11:00:00')
    headers = issue(['read', 'write:printers'])
    set_clock(monkeypatch, datetime(2020, 1, 1, 12, tzinfo=timezone.utc))
    client.post('/printers/199/plate-cleared', headers=headers)
    assert read(client, headers, 'detail')['plate_clear']['needed'] is False
    print_record(db, 902, start='2020-01-01 13:00:00', end='2020-01-01 14:00:00')
    for surface in ['detail', 'list', 'feed']:
        plate = read(client, headers, surface)['plate_clear']
        assert plate['needed'] is True and '14:00:00' in plate['last_print_ended_at']
