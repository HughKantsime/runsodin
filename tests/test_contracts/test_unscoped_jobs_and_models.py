"""Organization write boundaries through the job and library HTTP routes."""
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from tests.test_contracts.test_cert_D_jobsA_approval_workflow import cert, review_db, signed_license
from tests.test_contracts.test_cert_D_jobs_lifecycle_idempotency import owned_completion_background
from tests.test_contracts.test_education_classes_migration import pg_engine
from tests.test_contracts.test_scoped_cancel_no_start import BODY as NO_START, receipt


def detail(noun):
    return f'This {noun} belongs to no organization. Ask a platform administrator to change it.'


# role, actor group, has organizations, resource org, expected write status
SCOPES = [
    ('admin', 1, True, None, 403), ('operator1', 1, True, None, 403),
    ('admin', 1, True, 2, 404), ('admin', None, True, 2, 200),
    ('operator1', None, True, 2, 404), ('admin', 77, True, 2, 404),
    ('admin', None, True, None, 200), ('operator1', None, True, None, 200),
    ('admin', 77, True, None, 200), ('operator1', 77, True, None, 200),
    ('admin', None, False, None, 200), ('operator1', None, False, None, 200),
]


def principal(db, as_user, username, group, has_orgs):
    db.execute(text("INSERT INTO groups(id,name,is_org) VALUES(77,'Work group',FALSE)"))
    if not has_orgs:
        db.execute(text('UPDATE groups SET is_org=FALSE'))
    db.execute(text("UPDATE users SET group_id=:group WHERE username=:name"), {'group': group, 'name': username})
    db.commit()
    return as_user(username)


def check(response, expected, noun=None):
    if expected == 200:
        assert 200 <= response.status_code < 300, (response.request.url.path, response.text)
    else:
        assert response.status_code == expected, (response.request.url.path, response.text)
        if expected == 403 and noun:
            assert response.json()['detail'] == detail(noun)


@pytest.mark.parametrize('username,group,organizations,owner,status', SCOPES)
def test_library_writes(cert, review_db, username, group, organizations, owner, status):
    from modules.models_library.models import Model
    from modules.models_library.routes import router
    client, as_user, _ = cert
    client.app.include_router(router)
    headers = principal(review_db, as_user, username, group, organizations)
    review_db.add(Model(id=721, name='Bracket', org_id=owner))
    review_db.add(Model(id=724, name='Unused bracket', org_id=owner))
    review_db.flush()
    review_db.execute(text('INSERT INTO print_files(id,model_id,org_id,original_filename) VALUES '
                           "(722,721,:org,'variant.gcode'),(723,NULL,:org,'fixture.gcode')"), {'org': owner})
    review_db.commit()
    for path in ('/models/721', '/models/721/revisions', '/models/721/variants', '/print-files/723'):
        check(client.get(path, headers=headers), 404 if status == 404 else 200)
    for method, path, body, noun, admin_only in (
        ('patch', '/models/721', {'name': 'New bracket'}, 'model', False),
        ('post', '/models/721/revisions', None, 'model', False),
        ('post', '/models/721/revisions/1/revert', None, 'model', False),
        ('post', '/models/721/hide', None, 'model', False),
        ('delete', '/models/721/variants/722', None, 'model', True),
        ('patch', '/print-files/723/name', {'original_filename': 'new.gcode'}, 'file', False),
        ('delete', '/print-files/723', None, 'file', False),
        ('delete', '/models/724', None, 'model', True),
    ):
        role_denied = username == 'operator1' and admin_only
        check(client.request(method, path, json=body, headers=headers),
              403 if role_denied else status, None if role_denied else noun)


@pytest.mark.parametrize('username,group,organizations,owner,status,creator', [
    (*scenario, False) for scenario in SCOPES
] + [('operator1', 1, True, None, 200, True), ('admin', 1, True, 2, 404, True),
     ('operator1', 1, True, 1, 200, False)])
def test_job_writes(cert, review_db, owned_completion_background, monkeypatch,
                    username, group, organizations, owner, status, creator):
    from modules.jobs import no_start, outcome_correction
    from modules.jobs.routes import print_failure
    from modules.jobs.models import Job
    from modules.printers import dispatch
    client, as_user, _ = cert
    for module in (no_start, outcome_correction, print_failure):
        client.app.include_router(module.router)
    headers = principal(review_db, as_user, username, group, organizations)
    actor_id = 3 if username == 'admin' else 6
    submitter = actor_id if creator else 2
    review_db.execute(text("INSERT INTO printers(id,name,is_active,api_type) VALUES(720,'Bench printer',TRUE,'moonraker')"))
    review_db.add(Job(id=721, item_name='Bracket', charged_to_org_id=owner, submitted_by=submitter,
                      charged_to_user_id=actor_id, printer_id=720, duration_hours=1))
    review_db.commit()
    stage = Mock(return_value={'state': 'preparing'})
    monkeypatch.setattr(dispatch, 'stage_dispatch', stage)
    check(client.get('/jobs/721', headers=headers), 404 if status == 404 else 200)

    def write(method, path, body=None, expected=status):
        response = client.request(method, path, json=body, headers=headers)
        check(response, expected, 'job')
        return response

    def state(value, hold=False):
        review_db.execute(text('UPDATE jobs SET status=:value,hold=:hold,printer_id=720 WHERE id=721'),
                          {'value': value, 'hold': hold})
        review_db.commit()

    for method, suffix, initial, body in (
        ('patch', '', 'pending', {'notes': 'Updated'}),
        ('patch', '/move', 'pending', {'printer_id': 720, 'scheduled_start': '2030-01-01T00:00:00Z'}),
        ('post', '/dispatch', 'scheduled', None), ('post', '/start', 'scheduled', None),
        ('post', '/fail', 'printing', None), ('patch', '/failure', 'failed', {'fail_notes': 'Cleared'}),
        ('post', '/complete', 'printing', None), ('post', '/reset', 'failed', None),
        ('post', '/cancel', 'pending', None), ('post', '/approve', 'submitted', None),
        ('post', '/reject', 'submitted', {'reason': 'Revise'}),
    ):
        state(initial)
        write(method, '/jobs/721' + suffix, body)
    assert stage.call_count == int(status == 200)
    state('rejected')
    if status == 200 and not creator:
        response = client.post('/jobs/721/resubmit', headers=headers)
        assert response.status_code == 403 and 'original submitter' in response.text
    else:
        write('post', '/jobs/721/resubmit')
    state('pending', hold=True)
    receipt(review_db, 'c' * 32, job=721, printer=720)
    review_db.commit()
    write('post', '/jobs/721/dispatch-attempts/no-start', NO_START)

    state('cancelled')
    review_db.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,status,started_at,ended_at) "
                           "VALUES(722,720,721,'cancelled',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
    review_db.execute(text("INSERT INTO print_archives(id,job_id,print_job_id,printer_id,status,ownership_state,org_id) "
                           "VALUES(722,721,722,720,'cancelled','known',NULL)"))
    review_db.commit()
    write('post', '/jobs/721/link-print?print_job_id=722')
    write('post', '/print-jobs/722/mark-failed', {'reason': 'printer_fault'})
    write('post', '/print-jobs/722/undo-failure')
    write('post', '/jobs/721/outcome', {'from_status': 'cancelled', 'status': 'failed'})
    write('delete', '/jobs/721')


@pytest.mark.parametrize('action', ['reorder', 'hold', 'unhold', 'reschedule', 'set_priority', 'cancel', 'delete'])
def test_bulk_reports_only_visible_refusals(cert, review_db, action):
    from modules.jobs.models import Job
    client, as_user, _ = cert
    for identifier, org, submitter in ((721, None, None), (722, None, 3), (723, 1, None), (724, 2, 3)):
        review_db.add(Job(id=identifier, item_name='Bracket', charged_to_org_id=org,
                          submitted_by=submitter, printer_id=9, hold=action == 'unhold'))
    review_db.commit()
    ids = [721, 722, 723, 724, 725]
    headers = as_user('admin')
    if action == 'reorder':
        response = client.patch('/jobs/reorder', headers=headers, json={'job_ids': ids})
    else:
        response = client.post('/jobs/bulk-update', headers=headers,
                               json={'action': action, 'priority': 1, 'job_ids': ids})
    check(response, 200)
    result = response.json()
    assert result.get('affected', result.get('reordered')) == 2
    if action == 'reschedule':
        assert result['skipped'] == 3
    assert result.get('skipped_items', result['skipped']) == [{'job_id': 721, 'detail': detail('job')}]
    review_db.expire_all()
    for untouched in (721, 724):
        row = review_db.get(Job, untouched)
        assert (row.status, row.priority, row.queue_position, row.hold, row.printer_id) == (
            'pending', 3, None, action == 'unhold', 9)


@pytest.mark.parametrize('model_org,file_org,noun', [(None, 1, 'model'), (1, None, 'file')])
def test_variant_checks_both_owners_on_either_delete_route(cert, review_db, model_org, file_org, noun):
    from modules.models_library.routes import router
    client, as_user, _ = cert
    client.app.include_router(router)
    review_db.execute(text("INSERT INTO models(id,name,org_id) VALUES(731,'Bracket',:org)"), {'org': model_org})
    review_db.execute(text("INSERT INTO print_files(id,model_id,org_id) VALUES(732,731,:org)"), {'org': file_org})
    review_db.commit()
    for path in ('/models/731/variants/732', '/print-files/732'):
        check(client.delete(path, headers=as_user('admin')), 403, noun)
    assert review_db.execute(text('SELECT model_id FROM print_files WHERE id=732')).scalar_one() == 731


@pytest.mark.parametrize('dialect', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgresql)])
def test_print_correction_permission_reflects_the_linked_job(dialect, request, tmp_path):
    from core.schema import bootstrap_database
    from modules.jobs.print_failure import annotate

    if dialect == 'postgresql':
        engine = request.getfixturevalue('pg_engine')
    else:
        engine = create_engine('sqlite:///' + str(tmp_path / 'corrections.db'))
        request.addfinalizer(engine.dispose)
    bootstrap_database(engine)
    with Session(engine) as db:
        db.execute(text("INSERT INTO printers(id,name) VALUES(720,'Bench printer')"))
        db.execute(text("INSERT INTO jobs(id,item_name,submitted_by) VALUES(721,'Bracket',3)"))
        db.execute(text('INSERT INTO print_jobs(id,printer_id,scheduled_job_id,started_at) VALUES '
                        '(722,720,721,CURRENT_TIMESTAMP),(723,720,NULL,CURRENT_TIMESTAMP)'))
        rows = [{'id': 722}, {'id': 723}]
        for user_id, expected in [(3, True), (4, False)]:
            result = annotate(db, rows, user={'id': user_id, 'role': 'operator', 'group_id': 1})
            assert [r['id'] for r in result] == [722, 723]
            assert [r['failure_correction_allowed'] for r in result] == [expected, True]


def test_member_can_queue_and_repeat_unscoped_resources(cert, review_db, tmp_path):
    from modules.models_library.models import Model
    from modules.models_library.routes import router
    client, as_user, _ = cert
    client.app.include_router(router)
    headers = as_user('operator1')
    review_db.add(Model(id=721, name='Bracket', build_time_hours=1))
    review_db.flush()
    review_db.execute(text("INSERT INTO printers(id,name,api_type,is_active) VALUES(720,'Bench printer','moonraker',TRUE)"))
    path = tmp_path / 'bracket.gcode'
    path.write_text('; fixture')
    review_db.execute(text("INSERT INTO print_files(id,model_id,filename,original_filename,stored_path,filaments_json,blob_state) "
                           "VALUES(722,721,'bracket.gcode','bracket.gcode',:path,'[]','present')"), {'path': str(path)})
    review_db.commit()
    body = {'item_name': 'Bracket', 'model_id': 721, 'printer_id': 720}
    for url, payload in [('/jobs', body), ('/jobs/bulk', [body]),
                         ('/jobs/batch', {'item_name': 'Bracket', 'model_id': 721, 'printer_ids': [720]})]:
        check(client.post(url, headers=headers, json=payload), 200)
    check(client.post('/models/721/schedule?printer_id=720', headers=headers), 200)
    # Model scheduling can already have queued the file. Complete that work before scheduling again.
    review_db.execute(text("UPDATE jobs SET status='completed' WHERE model_id=721"))
    review_db.commit()
    queued = client.post('/print-files/722/schedule?printer_id=720', headers=headers)
    check(queued, 200)
    job_id = queued.json()['job_id']
    repeated = client.post(f'/jobs/{job_id}/repeat', headers=headers)
    check(repeated, 200)
    ids = review_db.execute(text('SELECT charged_to_org_id,print_file_id FROM jobs WHERE id IN (:first,:second)'),
                            {'first': job_id, 'second': repeated.json()['new_job_id']}).all()
    assert ids == [(1, 722), (1, 722)]
