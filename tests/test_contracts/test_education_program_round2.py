"""Round-two privacy regressions: inference and live recipient program scope."""
from copy import deepcopy
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from modules.organizations.education_program_rendering import METRICS, safe_export
from modules.organizations.services import validate_program_schedule
from tests.test_contracts.test_education_program_schedules import scheduled
from tests.test_contracts.test_education_program_reports import program
from tests.test_contracts.test_education_classes import setup
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_education_program_round3 import derivable


LINKED = ('jobs', 'completed_jobs', 'failed_jobs', 'success_rate')


def hidden(value):
    return isinstance(value, str) and value.startswith('Suppressed')


def example():
    def row(**extra):
        return {**dict.fromkeys(METRICS, 20), **extra}
    return {'notice': 'Example.', 'summary': row(),
            'programs': [row(id=7), row(id=8)],
            'classes': [row(id='a', title='a', cost_center_id=7), row(id='b', title='b', cost_center_id=7),
                        row(id='c', title='c', cost_center_id=8)]}


@pytest.mark.parametrize('metric', LINKED)
def test_linked_counts_cannot_recover_small_cell_or_rate(metric):
    report = example()
    report['classes'][0].update(jobs=40, completed_jobs=20, failed_jobs=20, success_rate=50)
    report['classes'][0][metric] = 2
    original = deepcopy(report)
    result = safe_export(report)
    row = result['classes'][0]
    assert hidden(row['success_rate'])
    assert sum(hidden(row[key]) for key in LINKED[:-1]) >= 2
    assert row['unknown_hours_jobs'] == 20
    assert report == original


@pytest.mark.parametrize('metric', [key for key in METRICS if key != 'success_rate'])
@pytest.mark.parametrize('level', ['class', 'program'])
def test_subtraction_cannot_recover_suppressed_cell_at_either_level(metric, level):
    report = example()
    rows = report['classes'] if level == 'class' else report['programs']
    rows[0][metric] = 2
    if level == 'class':
        report['programs'][0][metric] = 22
        report['summary'][metric] = 42
    else:
        report['summary'][metric] = 22
    result = safe_export(report)
    assert not derivable(result, metric, {'a'} if level == 'class' else {'a', 'b'})


def test_unassigned_job_count_does_not_reveal_hidden_class_jobs():
    report = example()
    report['classes'][0]['jobs'] = 2
    report['programs'][0].update(jobs=28, unassigned_jobs=6)
    result = safe_export(report)
    assert hidden(result['programs'][0]['jobs'])
    assert not derivable(result, 'jobs', {'a'})


def test_small_unassigned_count_leaves_unrelated_status_metrics_visible():
    report = example()
    report['programs'][0].update(jobs=42, unassigned_jobs=2)
    result = safe_export(report)
    assert hidden(result['programs'][0]['unassigned_jobs'])
    assert all(result['programs'][0][key] == report['programs'][0][key] for key in LINKED)


def test_large_cells_remain_available():
    report = example()
    result = safe_export(report)
    assert result['summary'] == report['summary']
    assert result['programs'] == report['programs']
    assert result['classes'] == report['classes']


@pytest.mark.parametrize('scope', [{}, {'term': 'Fall'}, {'cost_center_id': 8}, {'class_id': 'other-class'}])
def test_manager_cannot_receive_unscoped_or_other_program(scheduled, scope):
    db, cls, _ = scheduled
    add_other_program(db, cls)
    with pytest.raises(HTTPException) as error:
        validate_program_schedule(db, {'org_id': 1, **scope}, 6, ['manager@example.test'])
    assert error.value.status_code == 422


def add_other_program(db, cls):
    db.execute(text("INSERT INTO education_cost_centers "
        "(id,org_id,name_key,code_key,display_name,code,state,revision,created_by) "
        "VALUES(8,1,'art','art','Art','ART','active',1,3)"))
    db.execute(text("INSERT INTO education_classes(id,org_id,cost_center_id,title,term,created_by) "
        "VALUES('other-class',1,8,'Art',:term,2)"), {'term': cls['term']})
    db.commit()


@pytest.mark.parametrize('scope', ['program', 'class', 'both', 'admin_all', 'admin_term'])
def test_authorized_recipient_scope(scheduled, scope):
    db, cls, _ = scheduled
    add_other_program(db, cls)
    config = {'org_id': 1}
    recipients = ['manager@example.test', 'admin@example.test']
    if scope in ('program', 'both'):
        config['cost_center_id'] = 7
    if scope in ('class', 'both'):
        config['class_id'] = cls['id']
    if scope.startswith('admin'):
        recipients = ['admin@example.test']
    if scope == 'admin_term':
        config['term'] = cls['term']
    validate_program_schedule(db, config, 6, recipients)


def test_every_recipient_must_manage_selected_program(scheduled):
    db, cls, _ = scheduled
    add_other_program(db, cls)
    db.execute(text("INSERT INTO education_cost_center_grants "
        "(org_id,cost_center_id,user_id,role,state,granted_by) VALUES(1,8,4,'manager','active',3)"))
    db.commit()
    with pytest.raises(HTTPException) as error:
        validate_program_schedule(db, {'org_id': 1, 'cost_center_id': 7}, 6,
            ['admin@example.test', 'manager@example.test', 'outsider@example.test'])
    assert error.value.status_code == 422


@pytest.mark.parametrize('mode', ['poll', 'now'])
def test_send_rechecks_program_scope_after_render(scheduled, monkeypatch, mode):
    from modules.reporting import report_runner as runner
    db, cls, config = scheduled
    add_other_program(db, cls)
    config['class_id'] = cls['id']
    config['cost_center_id'] = None
    db.execute(text('UPDATE report_schedules SET filters=:f WHERE id=98'), {'f': json.dumps(config)})
    db.commit()
    monkeypatch.setattr(runner, 'SessionLocal', lambda: Session(db.get_bind()))
    monkeypatch.setattr(runner, 'get_smtp_config', lambda session: {'fake': True})

    def render(session, filters):
        # The recipient remains a manager, but of a different program now.
        session.execute(text("UPDATE education_cost_center_grants SET state='revoked' WHERE user_id=2"))
        session.execute(text("INSERT INTO education_cost_center_grants "
            "(org_id,cost_center_id,user_id,role,state,granted_by) VALUES(1,8,2,'manager','active',3)"))
        session.commit()
        return '<p>rendered</p>'

    monkeypatch.setitem(runner.GENERATORS, 'program_report', render)
    sent = []
    monkeypatch.setattr(runner, 'send_report_email', lambda *args: sent.append(args) or True)
    if mode == 'poll':
        runner.process_due_reports(db)
    else:
        row = dict(db.execute(text('SELECT * FROM report_schedules WHERE id=98')).mappings().one())
        with pytest.raises(HTTPException):
            runner.run_report(row)
    assert sent == []
