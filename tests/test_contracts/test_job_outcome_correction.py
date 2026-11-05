import json
import pytest
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db


@pytest.fixture
def correction_client(token_client, review_db, monkeypatch):
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.jobs.outcome_correction import router
    registry.register_provider('EducationPolicyProvider', EducationPolicyService())
    client, issue = token_client
    client.app.include_router(router)
    review_db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id) VALUES(901,'Synthetic correction','failed',1)"))
    review_db.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,status,started_at,ended_at) VALUES(902,9,901,'failed',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
    review_db.execute(text("INSERT INTO print_archives(id,job_id,print_job_id,printer_id,status,cost_estimate,filament_used_grams,ownership_state,org_id) VALUES(903,901,902,9,'failed',4.25,50,'known',1)"))
    review_db.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(901,'Synthetic','Correction spool','PLA')"))
    review_db.execute(text("INSERT INTO spools(id,filament_id,status,org_id,remaining_weight_g) VALUES(901,901,'active',1,500)"))
    review_db.commit()
    return client, issue


def snapshot(db):
    # All persisted evidence besides the three intended statuses and audit must remain byte-identical.
    tables = ['filament_ledger', 'spools', 'alerts', 'alert_outbox', 'job_summary_outbox',
              'terminal_filament_measurements', 'filament_cost_events', 'job_timeline_events']
    present = set(db.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).scalars())
    return {table: list(db.execute(text('SELECT * FROM ' + table))) for table in tables if table in present}


def test_reclassifies_both_directions_without_accounting_or_alert_side_effects(correction_client, review_db):
    client, issue = correction_client
    headers = issue(['write:jobs'])
    before = snapshot(review_db)
    for source, target in [('failed','cancelled'),('cancelled','failed')]:
        response = client.post('/jobs/901/outcome', headers=headers, json={'from_status':source,'status':target,'print_id':902})
        assert response.status_code == 200, response.text
        assert review_db.execute(text('SELECT status FROM jobs WHERE id=901')).scalar_one() == target
        assert review_db.execute(text('SELECT status FROM print_jobs WHERE id=902')).scalar_one() == target
        assert tuple(review_db.execute(text('SELECT status,cost_estimate,filament_used_grams FROM print_archives WHERE id=903')).one()) == (target,4.25,50)
        assert snapshot(review_db) == before
    details = review_db.execute(text("SELECT details FROM audit_logs WHERE action='job.outcome_reclassified' ORDER BY id")).scalars().all()
    assert [json.loads(value) if isinstance(value,str) else value for value in details] == [
        {'from':'failed','to':'cancelled','print_id':902}, {'from':'cancelled','to':'failed','print_id':902}]


@pytest.mark.parametrize('status', ['printing','paused','pending','scheduled','completed','cancelled'])
def test_nonmatching_or_nonterminal_job_cannot_be_corrected(correction_client, review_db, status):
    client, issue = correction_client
    review_db.execute(text('UPDATE jobs SET status=:status WHERE id=901'), {'status':status}); review_db.commit()
    response = client.post('/jobs/901/outcome', headers=issue(['write:jobs']), json={'from_status':'failed','status':'cancelled'})
    assert response.status_code == 409
    assert review_db.execute(text('SELECT status FROM print_jobs WHERE id=902')).scalar_one() == 'failed'


@pytest.mark.parametrize('body', [{'status':'completed','from_status':'failed'}, {'status':'failed','from_status':'failed'},
                                {'status':'cancelled','from_status':'failed','print_id':999}])
def test_bad_pair_or_attempt_is_rejected_atomically(correction_client, review_db, body):
    client, issue = correction_client
    assert client.post('/jobs/901/outcome', headers=issue(['write:jobs']), json=body).status_code in (400,409,422)
    assert review_db.execute(text('SELECT status FROM jobs WHERE id=901')).scalar_one() == 'failed'


@pytest.mark.parametrize('role,org,scope,expected', [('viewer',1,'write:jobs',403),('operator',1,'read:jobs',403),('operator',2,'write:jobs',404)])
def test_correction_requires_jobs_edit_and_owner(correction_client, review_db, role, org, scope, expected):
    client, issue = correction_client
    review_db.execute(text('UPDATE users SET role=:role,group_id=:org WHERE id=3'), {'role':role,'org':org}); review_db.commit()
    assert client.post('/jobs/901/outcome', headers=issue([scope]), json={'from_status':'failed','status':'cancelled'}).status_code == expected
    assert review_db.execute(text('SELECT status FROM jobs WHERE id=901')).scalar_one() == 'failed'


def test_correction_cannot_change_a_foreign_retained_archive(correction_client, review_db):
    client, issue = correction_client
    review_db.execute(text('UPDATE print_archives SET org_id=2 WHERE id=903')); review_db.commit()
    response = client.post('/jobs/901/outcome', headers=issue(['write:jobs']), json={'from_status':'failed','status':'cancelled'})
    assert response.status_code == 404
    assert review_db.execute(text('SELECT status FROM jobs WHERE id=901')).scalar_one() == 'failed'
    assert review_db.execute(text('SELECT status FROM print_jobs WHERE id=902')).scalar_one() == 'failed'
