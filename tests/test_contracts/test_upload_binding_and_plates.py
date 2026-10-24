"""Upload destination policy and sparse single-plate queue/dispatch contracts."""
import io
import zipfile
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from core.errors import OdinError
from core.print_file_binding import bind_file, target_printer, require_bound_target
from tests.test_contracts.test_cert_C_models import library, sliced_3mf
from tests.test_contracts.test_api_token_delegation_behavior import token_client
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.dispatch_test_support import configure_dispatch


def sparse_package():
    original = zipfile.ZipFile(io.BytesIO(sliced_3mf(plates=1)))
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as archive:
        for name in original.namelist():
            archive.writestr(name, original.read(name))
        archive.writestr('Metadata/plate_4.gcode', original.read('Metadata/plate_1.gcode'))
        archive.writestr('Metadata/plate_7.json', '{"bbox_objects":[]}')
    return out.getvalue()


def test_sparse_parser_includes_unsliced_plate():
    from modules.models_library.threemf_parser import plate_summaries
    with zipfile.ZipFile(io.BytesIO(sparse_package())) as archive:
        summaries = plate_summaries(archive)
    assert [(p['plate_index'], p['is_sliced']) for p in summaries] == [(0, True), (3, True), (6, False)]
    assert summaries[0]['print_time_seconds'] == 5430
    assert summaries[0]['filament_grams'] == 41.37


def test_binding_storage_is_immutable_and_unbound_is_unchanged(review_db):
    assert require_bound_target(review_db, 11, 99) is None
    bind_file(review_db, 11, 9)
    assert target_printer(review_db, 11) == 9
    assert require_bound_target(review_db, 11, 9) == 9
    for wrong in (None, 10):
        with pytest.raises(OdinError):
            require_bound_target(review_db, 11, wrong)
    with pytest.raises(OdinError):
        bind_file(review_db, 11, 10)


def test_upload_binding_schedule_and_sparse_plate(library, review_db):
    client, issue = library
    headers = issue(['write:models', 'write:jobs', 'read:models'])
    review_db.execute(text("UPDATE printers SET api_type='bambu',is_active=1 WHERE id=9"))
    review_db.commit()
    def upload(target=9):
        return client.post('/print-files/upload', headers=headers,
            data={} if target is None else {'target_printer_id': str(target)},
            files={'file': ('bound.3mf', sparse_package(), 'application/octet-stream')})
    first = upload()
    assert first.status_code == 200, first.text
    body = first.json()
    assert body['target_printer_id'] == 9
    assert target_printer(review_db, body['id']) == 9
    second = upload().json()
    assert second['model_id'] != body['model_id']
    assert upload(99999).status_code == 404
    review_db.execute(text("INSERT INTO printers(id,name,org_id,shared,is_active,api_type) VALUES(19,'Foreign target',2,0,1,'bambu')"))
    review_db.commit()
    assert upload(19).status_code == 404  # foreign tenant
    mid = body['model_id']
    options = client.get(f'/models/{mid}/schedule-options', headers=headers).json()
    assert options['plate_indices'] == [0, 3]
    plates = client.get(f'/models/{mid}/plates', headers=headers)
    assert plates.status_code == 200, plates.text
    assert plates.json()['target_printer_id'] == 9
    assert [(p['plate_index'],p['is_sliced']) for p in plates.json()['plates']] == [(0,True),(3,True),(6,False)]
    payload = {k: options[k] for k in ('print_file_id', 'payload_sha256')}
    payload['plate_index'] = 3
    wrong = client.post(f'/models/{mid}/schedule?printer_id=10', headers=headers, json=payload)
    assert wrong.status_code == 409, wrong.text
    refused = client.post(f'/models/{mid}/schedule', headers=headers, json=payload)
    assert refused.status_code == 409, refused.text
    assert client.post(f'/models/{mid}/schedule?printer_id=9', headers=headers, json={**payload,'plate_index':6}).status_code == 400
    scheduled = client.post(f'/models/{mid}/schedule?printer_id=9', headers=headers, json=payload)
    assert scheduled.status_code == 200, scheduled.text
    job = review_db.execute(text('SELECT * FROM jobs WHERE model_id=:id'), {'id':mid}).mappings().one()
    assert job['plate_index'] == 3 and job['print_file_id'] == body['id']
    assert job['printer_id'] == 9 and job['target_filter'] == '9'
    from modules.jobs.routes import jobs_crud
    client.app.include_router(jobs_crud.router)
    changed = client.patch(f"/jobs/{job['id']}", headers=headers, json={'printer_id':10,'pinned_printer_id':10})
    assert changed.status_code == 409, changed.text
    assert client.patch(f"/jobs/{job['id']}", headers=headers, json={'pinned_printer_id':None}).status_code == 409


def test_patch_cannot_retarget_or_unpin_bound_file(review_db):
    from modules.jobs.pinning import _pin_checked_changes
    from modules.jobs.models import Job
    from modules.jobs.schemas import JobUpdate
    bind_file(review_db, 11, 9)
    job = review_db.query(Job).filter(Job.id == 13).one()
    for changes in ({'pinned_printer_id':None}, {'printer_id':None}):
        with pytest.raises(OdinError):
            _pin_checked_changes(review_db, {'id':1,'role':'admin','group_id':None}, job, JobUpdate(**changes))


def test_dispatch_reloads_binding_and_refuses_wrong_target(review_db, tmp_path, monkeypatch):
    from modules.printers import dispatch
    configure_dispatch(review_db, tmp_path, monkeypatch)
    bind_file(review_db, 52, 9)
    review_db.commit()
    job = dispatch._load_job(53)
    assert job['upload_target_printer_id'] == 9
    options, reason = dispatch._validated_print_options(job, {'api_type':'moonraker'}, job['stored_path'], 'a'*64)
    assert options is None and 'bound to another printer' in reason


def test_dispatch_sends_sparse_selected_plate(tmp_path):
    from modules.printers.dispatch import _validated_print_options
    path = tmp_path / 'sparse.3mf'
    path.write_bytes(sparse_package())
    options, reason = _validated_print_options({'printer_id':9, 'upload_target_printer_id':9,
        'print_file_id':11, 'payload_sha256':'a'*64, 'plate_index':3}, {'api_type':'bambu'}, str(path), 'a'*64)
    assert reason is None and options['plate_num'] == 4


@pytest.mark.parametrize('bound', [None, 59, 9])
def test_real_dispatch_enforces_binding_before_adapter(review_db, tmp_path, monkeypatch, bound):
    from modules.printers import dispatch
    configure_dispatch(review_db, tmp_path, monkeypatch)
    bind_file(review_db, 52, bound)
    review_db.commit()
    calls = []
    def adapter(*args, **kwargs):
        calls.append(args)
        return True, 'Synthetic accepted command'
    monkeypatch.setattr(dispatch, '_dispatch_moonraker', adapter)
    ok, reason = dispatch.dispatch_job(59, 53)
    assert ok == (bound in (None, 59)), reason
    assert len(calls) == (1 if ok else 0)
    if not ok:
        assert 'bound to another printer' in reason


def test_real_dispatch_passes_selected_sparse_plate_to_adapter(review_db, tmp_path, monkeypatch):
    import hashlib
    from modules.printers import dispatch
    engine, path = configure_dispatch(review_db, tmp_path, monkeypatch, 'bambu')
    path.write_bytes(sparse_package())
    with engine.begin() as conn:
        conn.execute(text('UPDATE jobs SET plate_index=3,payload_sha256=:hash WHERE id=53'),
                     {'hash':hashlib.sha256(path.read_bytes()).hexdigest()})
    bind_file(review_db, 52, 59)
    review_db.commit()
    seen = []
    def adapter(jid, staged, remote, creds, **options):
        seen.append(options['plate_num'])
        return True, 'Synthetic accepted command'
    monkeypatch.setattr(dispatch, '_dispatch_bambu', adapter)
    ok, reason = dispatch.dispatch_job(59, 53)
    assert ok, reason
    assert seen == [4]


def test_raw_bound_uploads_are_separate_and_unbound_uploads_keep_merging(library, review_db):
    client, issue = library
    review_db.execute(text("UPDATE printers SET api_type='moonraker' WHERE id IN (9,10)"))
    review_db.commit()
    headers = issue(['write:models', 'write:jobs'])
    def upload(target=None):
        response = client.post('/print-files/upload', headers=headers,
            data={} if target is None else {'target_printer_id':str(target)},
            files={'file':('unique-raw-binding.gcode',b'G90\n','application/octet-stream')})
        assert response.status_code == 200, response.text
        return response.json()
    bound = upload(9)
    unbound = upload()
    assert bound['model_id'] != unbound['model_id']
    assert upload()['model_id'] == unbound['model_id']
    assert target_printer(review_db, unbound['id']) is None
    assert client.post(f"/print-files/{bound['id']}/schedule?printer_id=10", headers=headers).status_code == 409
    assert client.post(f"/print-files/{bound['id']}/schedule?printer_id=9", headers=headers).status_code == 200
    assert client.post(f"/models/{unbound['model_id']}/schedule", headers=headers).status_code == 200


@pytest.mark.parametrize('bound', [9, 10])
def test_scheduler_never_places_bound_file_elsewhere_even_without_pin(review_db, monkeypatch, bound):
    from datetime import datetime, timezone
    from modules.jobs.scheduler import Scheduler, SchedulerConfig
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    review_db.execute(text("INSERT INTO models(id,name,org_id) VALUES(901,'Synthetic bound model',1)"))
    review_db.execute(text("INSERT INTO print_files(id,model_id,filename,org_id) VALUES(902,901,'bound.gcode',1)"))
    review_db.execute(text("UPDATE models SET print_file_id=902 WHERE id=901"))
    review_db.execute(text("INSERT INTO jobs(id,model_id,item_name,status,charged_to_org_id,quantity,duration_hours,hold,priority) VALUES(903,901,'Synthetic bound job','pending',1,1,1,FALSE,3)"))
    review_db.execute(text("UPDATE printers SET is_active=FALSE WHERE id=10"))
    bind_file(review_db, 902, bound)
    review_db.commit()
    result = Scheduler(SchedulerConfig(horizon_days=1)).run(review_db, start_date=datetime(2026,10,12,12,tzinfo=timezone.utc))
    assert result.scheduled_count == (1 if bound == 9 else 0)
    actual = review_db.execute(text('SELECT printer_id FROM jobs WHERE id=903')).scalar()
    assert actual == (9 if bound == 9 else None)


def test_general_upload_shared_printer_does_not_relax_studio_access(library, review_db):
    from modules.models_library.routes.print_files import _get_accessible_printer
    client, issue = library
    review_db.execute(text("INSERT INTO printers(id,name,org_id,shared,is_active,api_type) VALUES(19,'Shared target',2,1,1,'moonraker')"))
    review_db.commit()
    principal = {'id':3,'role':'admin','group_id':1}
    with pytest.raises(OdinError):
        _get_accessible_printer(review_db, 19, principal)
    assert _get_accessible_printer(review_db, 19, principal, allow_shared=True).id == 19
    response = client.post('/print-files/upload', headers=issue(['write:models']),
        data={'target_printer_id':'19'}, files={'file':('shared.gcode',b'G90\n','application/octet-stream')})
    assert response.status_code == 200, response.text
    assert response.json()['target_printer_id'] == 19
