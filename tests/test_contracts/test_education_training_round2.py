"""Second-review regressions for training authority, edits, locking and scale."""
import asyncio
from uuid import uuid4
from unittest.mock import MagicMock
import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session
from core.errors import OdinError
from modules.organizations import education_training_service as s
from modules.organizations import education_training_policy as policy
from modules.organizations.education_training_schemas import TrainingUpdate
from tests.test_contracts.test_education_training import db, create, definition, quiz, sign, QUIZ
from tests.test_contracts.test_education_review_workflow import review_db, _principal


def update_body(**changes):
    return TrainingUpdate(**{**definition().model_dump(), **changes}, revision=1, edit_version=1)


def test_manager_cannot_create_school_wide_even_with_forged_admin_claim(db):
    with pytest.raises(OdinError) as error:
        s.save_training(db, _principal(2, 'admin'), definition(school_wide=True), center_id=7)
    assert error.value.status == 403


@pytest.mark.parametrize('old_school,new_school', [(False, True), (True, True), (True, False)])
def test_manager_cannot_edit_school_wide_or_toggle_scope(db, old_school, new_school):
    t = s.save_training(db, _principal(3, 'admin'), definition(school_wide=old_school), center_id=7)
    with pytest.raises(OdinError) as error:
        s.save_training(db, _principal(2), update_body(school_wide=new_school), training_id=t['id'])
    assert error.value.status == 403


def test_text_edits_have_independent_version_and_stale_editor_gets_conflict(db):
    t = create(db); sign(db, t)
    body = update_body(description='New instructions')
    saved = s.save_training(db, _principal(2), body, training_id=t['id'])
    assert saved['revision'] == 1 and saved['edit_version'] == 2
    assert s.save_training(db, _principal(2), body, training_id=t['id']) == saved
    with pytest.raises(OdinError) as error:
        s.save_training(db, _principal(2), update_body(name='Stale rename'), training_id=t['id'])
    assert error.value.status == 409
    db.rollback()
    assert s.list_trainings(db, _principal(1), 7)['items'][0]['status'] == 'complete'


def test_passed_quiz_cannot_be_replaced_while_waiting_for_signoff(db):
    t = create(db, quiz=QUIZ)
    assert quiz(db, t, [1, 0])['status'] == 'instructor_required'
    with pytest.raises(OdinError) as error:
        quiz(db, t, [0, 1])
    assert error.value.status == 409
    db.rollback()
    row = db.execute(text('SELECT quiz_attempts,quiz_score FROM education_training_records')).one()
    assert tuple(row) == (1, 100)
    assert sign(db, t)['status'] == 'complete'


def capture_queries(db, call):
    queries = []
    def capture(conn, cursor, sql, params, context, many):
        queries.append(sql)
    event.listen(db.get_bind(), 'before_cursor_execute', capture)
    try:
        result = call()
    finally:
        event.remove(db.get_bind(), 'before_cursor_execute', capture)
    return result, queries


def test_upload_locks_school_before_class(db):
    from modules.organizations.education_submission_graph import _lock_job_birth_authority
    db.execute(text("UPDATE education_upload_operations SET state='renamed' WHERE operation_id='upload-1'")); db.commit()
    _, queries = capture_queries(db, lambda: _lock_job_birth_authority(db, 'upload-1', _principal(1), 7))
    school = next(i for i, sql in enumerate(queries) if 'UPDATE groups' in sql)
    center = next(i for i, sql in enumerate(queries) if 'education_cost_centers' in sql)
    assert school < center


@pytest.mark.parametrize('previous,bound', [('0','1000ms'), ('500','500ms'), ('4000','1000ms')])
def test_training_timeout_is_restored_after_authority_locks(monkeypatch, previous, bound):
    session = MagicMock(spec=Session)
    session.get_bind.return_value.dialect.name = 'postgresql'
    adapter = MagicMock()
    adapter.fetchone.return_value = {'setting': previous}
    monkeypatch.setattr(policy, '_adapter', lambda db: adapter)
    policy.lock_center(session, 7, 1)
    calls = adapter.execute.call_args_list
    configs = [call.args[1] for call in calls if 'set_config' in call.args[0]]
    assert configs == [{'bound': bound}, {'bound': previous + 'ms'}]
    assert 'UPDATE groups' in calls[1].args[0]
    assert 'UPDATE education_cost_centers' in calls[2].args[0]


def test_roster_query_count_does_not_grow_with_users(db):
    t = create(db)
    _, small = capture_queries(db, lambda: s.roster(db, _principal(2), t['id']))
    for user in range(20, 40):
        db.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES(:id,:name,'fixture','viewer',TRUE,1)"), {'id': user, 'name': f'student-{user}'})
        db.execute(text("INSERT INTO education_cost_center_grants(org_id,cost_center_id,user_id,role,state,granted_by) VALUES(1,7,:id,'student','active',3)"), {'id': user})
    db.commit()
    roster, large = capture_queries(db, lambda: s.roster(db, _principal(2), t['id']))
    assert len(roster['items']) == 21
    assert len(large) == len(small)
    assert all(item['active'] and item['record'] is None for item in roster['items'])


def test_class_printer_removal_warns_only_when_last_match_disappears(db):
    from modules.organizations.routes_education import replace_cost_center_printers
    from modules.organizations.education_schemas import PrinterReplacement
    create(db)
    async def replace(ids, revision):
        return await replace_cost_center_printers(7, PrinterReplacement(command_id=uuid4(), revision=revision, printer_ids=ids), _principal(3, 'admin'), db)
    assert not asyncio.run(replace([9], 1)).get('training_warnings')
    result = asyncio.run(replace([], 2))
    assert 'P1S safety' in result['training_warnings'][0]
    assert 'no longer matches any printer' in result['training_warnings'][0]


@pytest.mark.parametrize('last_match', [False, True])
def test_printer_delete_returns_scope_warning_only_for_last_school_match(db, monkeypatch, last_match):
    import json
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.printers.routes_crud import delete_printer
    db.execute(text("INSERT INTO printers(id,name,model,machine_type,is_active,shared,org_id) VALUES(11,'School machine','Unique model','P1S',TRUE,FALSE,1)"))
    if not last_match:
        db.execute(text("INSERT INTO printers(id,name,model,machine_type,is_active,shared,org_id) VALUES(12,'Spare','Unique model','P1S',TRUE,FALSE,1)"))
    db.commit()
    s.save_training(db, _principal(3, 'admin'), definition(school_wide=True, scope_kind='model', scope_value='Unique model'), center_id=7)
    original = registry.get_provider
    monkeypatch.setattr(registry, 'get_provider', lambda name: EducationPolicyService() if name == 'EducationPolicyProvider' else original(name))
    response = delete_printer(11, _principal(3, 'admin'), db)
    assert response.status_code == 204
    assert response.body == b''
    if last_match:
        assert 'P1S safety' in json.loads(response.headers['X-ODIN-Training-Warnings'])[0]
    else:
        assert 'X-ODIN-Training-Warnings' not in response.headers
    assert not db.execute(text('SELECT 1 FROM printers WHERE id=11')).first()
