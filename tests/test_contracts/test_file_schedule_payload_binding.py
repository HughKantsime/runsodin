"""Actual file schedule must reach readonly payload validation, never transport."""
import hashlib
import zipfile
import pytest
from sqlalchemy import text
from tests.test_contracts.test_economic_creator_wiring import creators
from tests.test_contracts.test_education_review_workflow import review_db


@pytest.mark.parametrize('replace_original', [False, True])
def test_queue_payload_validates_private_snapshot_and_refuses_original_drift(tmp_path, monkeypatch, replace_original):
    from types import SimpleNamespace
    from fastapi import HTTPException
    from modules.models_library import queue_payload
    source = tmp_path / 'owned-private.3mf'
    with zipfile.ZipFile(source, 'w') as package:
        package.writestr('Metadata/plate_1.gcode', '; owned first plate')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    row = SimpleNamespace(stored_path=str(source), file_hash=digest,
                          compatible_api_types='', bed_x_mm=None, bed_y_mm=None)
    target = SimpleNamespace(api_type='bambu', is_active=True, bed_x_mm=None, bed_y_mm=None)
    validator = queue_payload.validate_plate
    inspected = []
    def inspect(path, plate, api):
        assert path != str(source)
        assert hashlib.sha256(__import__('pathlib').Path(path).read_bytes()).hexdigest() == digest
        inspected.append(path)
        result = validator(path, plate, api)
        if replace_original:
            replacement = tmp_path / 'replacement.3mf'
            replacement.write_bytes(b'changed owned payload')
            replacement.replace(source)
        return result
    monkeypatch.setattr(queue_payload, 'validate_plate', inspect)
    if replace_original:
        with pytest.raises(HTTPException, match='409'):
            with queue_payload.verified_queue_payload(row, target):
                pytest.fail('Changed original must never reach queue admission')
    else:
        with queue_payload.verified_queue_payload(row, target) as actual:
            assert actual == digest
    assert inspected and not __import__('pathlib').Path(inspected[0]).exists()


@pytest.mark.parametrize('defect', ['hash_drift', 'missing_plate', 'duplicate_plate', 'symlink',
                                    'oversize', 'unsupported_api', 'inactive_target'])
def test_file_schedule_refuses_unusable_payload_without_queue_effects(creators, review_db, tmp_path, defect):
    from modules.models_library.routes.print_files import router
    from modules.models_library.models import Model
    client, auth, _, _ = creators
    client.app.include_router(router, prefix='/api')
    model = Model(name='Owned invalid payload', org_id=1)
    review_db.add(model); review_db.flush()
    source = tmp_path / 'owned-invalid.gcode.3mf'
    with zipfile.ZipFile(source, 'w') as package:
        member = 'Metadata/plate_2.gcode' if defect == 'missing_plate' else 'Metadata/plate_1.gcode'
        package.writestr(member, '; synthetic owned payload')
        if defect == 'duplicate_plate':
            with pytest.warns(UserWarning, match='Duplicate name'):
                package.writestr(member, '; ambiguous duplicate')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if defect == 'hash_drift': digest = '0' * 64
    if defect == 'oversize':
        # Sparse owned fixture exercises the real size guard without allocation.
        with source.open('r+b') as payload:
            payload.truncate(100 * 1024 * 1024 + 1)
    if defect == 'unsupported_api':
        review_db.execute(text("UPDATE printers SET api_type='elegoo' WHERE id=9"))
    if defect == 'inactive_target':
        review_db.execute(text('UPDATE printers SET is_active=0 WHERE id=9'))
    if defect == 'symlink':
        alias = tmp_path / 'alias.gcode.3mf'; alias.symlink_to(source); source = alias
    review_db.execute(text("INSERT INTO print_files(id,model_id,filename,original_filename,stored_path,org_id,filaments_json,file_hash) VALUES(501,:model,'owned-invalid.gcode.3mf','owned-invalid.gcode.3mf',:path,1,'[]',:sha)"), {'model': model.id, 'path': str(source), 'sha': digest})
    review_db.commit()
    before = tuple(review_db.execute(text('SELECT (SELECT count(*) FROM jobs),(SELECT count(*) FROM cost_identities),(SELECT quota_jobs FROM users WHERE id=3)')).one())
    response = client.post('/api/print-files/501/schedule?printer_id=9', headers=auth())
    assert response.status_code in (400, 409), response.text
    review_db.expire_all()
    assert tuple(review_db.execute(text('SELECT (SELECT count(*) FROM jobs),(SELECT count(*) FROM cost_identities),(SELECT quota_jobs FROM users WHERE id=3)')).one()) == before
    assert review_db.execute(text('SELECT job_id FROM print_files WHERE id=501')).scalar_one() is None


def test_scheduled_file_job_has_exact_payload_and_plate_identity(creators,review_db,tmp_path,monkeypatch):
    from modules.models_library.routes.print_files import router
    from modules.models_library.models import Model
    from modules.printers import dispatch
    from core import db as core_db
    client,auth,_,_=creators
    client.app.include_router(router,prefix="/api")
    model=Model(name="Owned file payload",org_id=1);review_db.add(model);review_db.flush()
    source=tmp_path/"owned.gcode.3mf"
    with zipfile.ZipFile(source,"w") as archive:archive.writestr("Metadata/plate_1.gcode","; owned local fixture, never hardware")
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    review_db.execute(text("INSERT INTO print_files(id,model_id,filename,original_filename,stored_path,org_id,filaments_json,file_hash) VALUES(501,:model,'owned.gcode.3mf','owned.gcode.3mf',:path,1,'[]',:sha)"),{"model":model.id,"path":str(source),"sha":digest});review_db.commit()
    response=client.post("/api/print-files/501/schedule?printer_id=9",headers=auth())
    assert response.status_code==200,response.text
    identifier=response.json()["job_id"]
    monkeypatch.setattr(core_db,"engine",review_db.get_bind())
    job=dispatch._load_job(identifier)
    options,error=dispatch._validated_print_options(job,{"api_type":"bambu"},str(source),digest)
    assert error is None,error
    assert options["plate_num"]==1
    assert job["payload_sha256"]==digest and job["plate_index"]==0



def test_studio_bambu_upload_persists_explicit_file_hash_and_plate(creators,review_db,tmp_path,monkeypatch):
    import io
    from modules.models_library.routes import print_files
    client,auth,_,_=creators
    client.app.include_router(print_files.router,prefix="/api")
    storage=tmp_path/"owned-studio-binding";storage.mkdir()
    monkeypatch.setenv("PRINT_FILE_STORAGE_ROOT",str(storage));print_files.limiter._storage.reset()
    from pathlib import Path
    source=Path(__file__).parents[1]/"fixtures/telemetry/a1-petg-source-sanitized.3mf"
    data=source.read_bytes();digest=hashlib.sha256(data).hexdigest()
    response=client.post("/api/prints",headers=auth(),data={"target_printer_id":"9"},files={"file":("owned-bambu.gcode.3mf",data,"application/octet-stream")})
    assert response.status_code==201,response.text
    identifier=int(response.json()["id"]);file_id=response.json()["file_id"]
    review_db.expire_all()
    assert review_db.execute(text("SELECT print_file_id,payload_sha256,plate_index FROM jobs WHERE id=:id"),{"id":identifier}).one()==(file_id,digest,0)
    # A later model primary or newest-file change cannot redirect this job.
    model_id = review_db.execute(text('SELECT model_id FROM print_files WHERE id=:id'), {'id': file_id}).scalar_one()
    replacement = tmp_path / 'different-primary.3mf'
    replacement.write_bytes(b'owned unrelated replacement')
    review_db.execute(text("INSERT INTO print_files(id,model_id,filename,original_filename,stored_path,org_id,filaments_json) VALUES(9999,:model,'replacement.3mf','replacement.3mf',:path,1,'[]')"), {'model': model_id, 'path': str(replacement)})
    review_db.execute(text('UPDATE models SET print_file_id=9999 WHERE id=:id'), {'id': model_id})
    review_db.commit()
    from core import db as core_db
    from modules.printers import dispatch
    monkeypatch.setattr(core_db, 'engine', review_db.get_bind())
    loaded = dispatch._load_job(identifier)
    assert loaded['print_file_id'] == file_id
    assert loaded['stored_path'] != str(replacement)
    options, error = dispatch._validated_print_options(loaded, {'api_type': 'bambu'}, loaded['stored_path'], digest)
    assert error is None, error
    assert options['plate_num'] == 1


def test_studio_target_change_during_upload_retains_upload_without_queue(creators, review_db, tmp_path, monkeypatch):
    from modules.models_library.routes import print_files
    from pathlib import Path
    client, auth, _, _ = creators
    client.app.include_router(print_files.router, prefix='/api')
    storage = tmp_path / 'owned-studio-target-drift'; storage.mkdir()
    monkeypatch.setenv('PRINT_FILE_STORAGE_ROOT', str(storage)); print_files.limiter._storage.reset()
    original = print_files.upload_3mf
    async def upload(**kwargs):
        result = await original(**kwargs)
        # Simulate a separate committed administrative change during upload.
        kwargs['db'].execute(text("UPDATE printers SET api_type='moonraker' WHERE id=9"))
        kwargs['db'].commit()
        return result
    monkeypatch.setattr(print_files, 'upload_3mf', upload)
    def counts():
        return tuple(review_db.execute(text('SELECT (SELECT count(*) FROM jobs),(SELECT count(*) FROM cost_identities),(SELECT coalesce(sum(jobs_used),0) FROM quota_usage WHERE user_id=3)')).one())
    before = counts(); review_db.commit()
    data = (Path(__file__).parents[1] / 'fixtures/telemetry/a1-petg-source-sanitized.3mf').read_bytes()
    response = client.post('/api/prints', headers=auth(), data={'target_printer_id': '9'}, files={'file': ('target-drift.3mf', data, 'application/octet-stream')})
    assert response.status_code == 409, response.text
    review_db.expire_all()
    assert counts() == before
    assert review_db.execute(text("SELECT count(*) FROM print_files WHERE original_filename='target-drift.3mf' AND job_id IS NULL")).scalar_one() == 1
    assert list(storage.iterdir())


@pytest.mark.parametrize('mutation', ['file_hash', 'model_owner', 'printer_api'])
def test_same_transaction_queue_reference_drift_rolls_back_job_and_identity(creators, review_db, tmp_path, monkeypatch, mutation):
    from modules.models_library.routes.print_files import router
    from modules.models_library.models import Model
    from modules.jobs import queue_creation_provider, register_providers
    from core.registry import registry
    monkeypatch.setattr(registry, '_providers', dict(registry._providers))
    register_providers(registry)
    client, auth, _, _ = creators
    client.app.include_router(router, prefix='/api')
    model = Model(name='Owned queue drift', org_id=1)
    review_db.add(model); review_db.flush()
    source = tmp_path / 'owned-drift.3mf'
    with zipfile.ZipFile(source, 'w') as package:
        package.writestr('Metadata/plate_1.gcode', '; owned first plate')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    review_db.execute(text("INSERT INTO print_files(id,model_id,filename,original_filename,stored_path,org_id,filaments_json,file_hash) VALUES(501,:model,'owned-drift.3mf','owned-drift.3mf',:path,1,'[]',:sha)"), {'model': model.id, 'path': str(source), 'sha': digest})
    review_db.commit()
    def counts():
        return tuple(review_db.execute(text('SELECT (SELECT count(*) FROM jobs),(SELECT count(*) FROM cost_identities),(SELECT coalesce(sum(jobs_used),0) FROM quota_usage WHERE user_id=3)')).one())
    before = counts(); review_db.commit()
    original = queue_creation_provider._stage_prospective_queue_values
    observed = []
    def stage(db, values, actor):
        job = original(db, values, actor)
        observed.append(job.id)
        statements = {
            'file_hash': "UPDATE print_files SET file_hash=:changed WHERE id=501",
            'model_owner': "UPDATE models SET org_id=NULL WHERE id=:model",
            'printer_api': "UPDATE printers SET api_type='moonraker' WHERE id=9",
        }
        db.execute(text(statements[mutation]), {'changed': '0' * 64, 'model': model.id})
        return job
    monkeypatch.setattr(queue_creation_provider, '_stage_prospective_queue_values', stage)
    response = client.post('/api/print-files/501/schedule?printer_id=9', headers=auth())
    assert response.status_code == 409, response.text
    assert len(observed) == 1
    review_db.expire_all()
    assert counts() == before
    assert review_db.execute(text('SELECT job_id,file_hash FROM print_files WHERE id=501')).one() == (None, digest)
    assert review_db.execute(text('SELECT org_id FROM models WHERE id=:id'), {'id': model.id}).scalar_one() == 1
    assert review_db.execute(text('SELECT api_type FROM printers WHERE id=9')).scalar_one() == 'bambu'
