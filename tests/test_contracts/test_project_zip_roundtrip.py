"""Advertised project restore must retain exported archive metadata and bytes (F02-2.8-38)."""
from pathlib import Path
import hashlib,io,json,zipfile
import pytest
from sqlalchemy import text
from tests.test_contracts.test_project_behavior import project_client
from tests.test_contracts.test_api_token_delegation_behavior import token_client,review_db


@pytest.fixture
def storage(tmp_path,monkeypatch):
    root=tmp_path/'print_files';root.mkdir()
    monkeypatch.setenv('PRINT_FILE_STORAGE_ROOT',str(root))
    return root


def _seed_file(review_db,storage,body=b'owned-project-archive-bytes',name='owned-print.3mf'):
    source=storage/name;source.write_bytes(body)
    review_db.execute(text("UPDATE print_archives SET project_id=101,file_path=:path,notes='kept note',tags='a,b',"
                           "actual_duration_seconds=3600,filament_used_grams=12.5,completed_at='2026-10-01 12:00:00',"
                           "cost_estimate=9.99 WHERE id=201"),{'path':str(source)})
    review_db.commit()
    return source


def _export(client,issue):
    response=client.get('/projects/101/export',headers=issue(['read:jobs']))
    assert response.status_code==200,response.text
    return response.content


def _import(client,issue,payload):
    return client.post('/projects/import',headers=issue(['write:jobs']),files={'file':('p.zip',payload,'application/zip')})


def _repack(payload,mutate):
    with zipfile.ZipFile(io.BytesIO(payload)) as package:
        members={name:package.read(name) for name in package.namelist()}
    manifest=json.loads(members['project.json']);mutate(manifest,members)
    members['project.json']=json.dumps(manifest).encode()
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as package:
        for name,data in members.items():package.writestr(name,data)
    return out.getvalue()


def _no_side_effects(review_db,storage):
    assert review_db.execute(text('SELECT COUNT(*) FROM projects')).scalar_one()==2
    assert review_db.execute(text("SELECT COUNT(*) FROM print_archives WHERE ownership_source='project_import'")).scalar_one()==0
    imports=storage/'project-imports'
    assert not imports.exists() or list(imports.iterdir())==[]


def test_owned_project_export_import_restores_archive_and_file(project_client,review_db,storage):
    client,issue=project_client
    source=_seed_file(review_db,storage)
    exported=_export(client,issue)
    with zipfile.ZipFile(io.BytesIO(exported)) as package:
        assert sorted(package.namelist())==['files/000.3mf','project.json']
        manifest=json.loads(package.read('project.json'))
        assert manifest['version']==2 and len(manifest['archives'])==1
        assert manifest['archives'][0]['file']['sha256']==hashlib.sha256(source.read_bytes()).hexdigest()
        assert package.read('files/000.3mf')==source.read_bytes()
        text_manifest=package.read('project.json').decode()
        assert str(storage) not in text_manifest and '9.99' not in text_manifest
    imported=_import(client,issue,exported)
    assert imported.status_code==200,imported.text
    body=imported.json();project=body['id']
    assert project!=101 and body['archives']==1 and body['files']==1
    assert review_db.execute(text('SELECT org_id,created_by FROM projects WHERE id=:p'),{'p':project}).one()==(1,3)
    restored=review_db.execute(text('SELECT * FROM print_archives WHERE project_id=:project'),{'project':project}).mappings().all()
    assert len(restored)==1,'Project import discarded exported archive history'
    row=restored[0]
    assert row['print_name']=='Own archive' and row['status']=='completed'
    assert row['notes']=='kept note' and row['tags']=='a,b'
    assert row['actual_duration_seconds']==3600 and row['filament_used_grams']==12.5
    assert str(row['completed_at']).startswith('2026-10-01 12:00:00')
    assert row['org_id']==1 and row['user_id']==3 and row['printer_id'] is None
    assert row['cost_estimate'] is None and row['accounting_state']=='unresolved'
    assert row['ownership_state']=='known' and row['ownership_source']=='project_import'
    restored_path=Path(row['file_path'])
    assert restored_path.read_bytes()==source.read_bytes() and restored_path!=source
    assert restored_path.parent.parent==storage/'project-imports'
    assert row['file_hash']==hashlib.sha256(source.read_bytes()).hexdigest()
    assert (restored_path.stat().st_mode&0o777)==0o600
    assert review_db.execute(text('SELECT file_path FROM print_archives WHERE id=201')).scalar_one()==str(source)


def test_file_outside_storage_root_is_exported_without_bytes(project_client,review_db,storage,tmp_path):
    client,issue=project_client
    outside=tmp_path/'outside.3mf';outside.write_bytes(b'not-in-root')
    review_db.execute(text('UPDATE print_archives SET project_id=101,file_path=:p WHERE id=201'),{'p':str(outside)});review_db.commit()
    with zipfile.ZipFile(io.BytesIO(_export(client,issue))) as package:
        assert package.namelist()==['project.json']
        assert json.loads(package.read('project.json'))['archives'][0]['file'] is None


def test_symlink_into_root_is_not_followed(project_client,review_db,storage,tmp_path):
    client,issue=project_client
    outside=tmp_path/'secret.3mf';outside.write_bytes(b'secret')
    link=storage/'link.3mf';link.symlink_to(outside)
    review_db.execute(text('UPDATE print_archives SET project_id=101,file_path=:p WHERE id=201'),{'p':str(link)});review_db.commit()
    with zipfile.ZipFile(io.BytesIO(_export(client,issue))) as package:
        assert package.namelist()==['project.json']


def test_non_terminal_archive_is_not_exported(project_client,review_db,storage):
    client,issue=project_client
    review_db.execute(text("UPDATE print_archives SET project_id=101,status='printing' WHERE id=201"));review_db.commit()
    with zipfile.ZipFile(io.BytesIO(_export(client,issue))) as package:
        assert json.loads(package.read('project.json'))['archives']==[]


def test_education_archive_exports_without_bytes_and_cannot_be_imported(project_client,review_db,storage):
    client,issue=project_client
    _seed_file(review_db,storage)
    def educate(manifest,members):
        manifest['archives'][0]['protection']='education'
        manifest['archives'][0]['file']=None;members.pop('files/000.3mf')
    payload=_repack(_export(client,issue),educate)
    response=_import(client,issue,payload)
    assert response.status_code==400 and 'Education' in response.json()['detail']
    _no_side_effects(review_db,storage)


@pytest.mark.parametrize('tamper',['hash','size','extra_member','traversal','legacy_v1','extra_key'])
def test_tampered_packages_are_refused_without_side_effects(project_client,review_db,storage,tamper):
    client,issue=project_client
    _seed_file(review_db,storage)
    exported=_export(client,issue)
    if tamper=='legacy_v1':
        out=io.BytesIO()
        with zipfile.ZipFile(out,'w') as package:package.writestr('project.json',json.dumps({'name':'Old export','archives':[]}))
        payload=out.getvalue()
    elif tamper=='traversal':
        out=io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(exported)) as source,zipfile.ZipFile(out,'w') as package:
            package.writestr('project.json',source.read('project.json'))
            package.writestr('../files/000.3mf',source.read('files/000.3mf'))
        payload=out.getvalue()
    else:
        def mutate(manifest,members):
            if tamper=='hash':members['files/000.3mf']=b'X'*len(members['files/000.3mf'])
            elif tamper=='size':members['files/000.3mf']+=b'!'
            elif tamper=='extra_member':members['files/001.3mf']=b'smuggled'
            elif tamper=='extra_key':manifest['archives'][0]['display']['cost_estimate']=1
        payload=_repack(exported,mutate)
    response=_import(client,issue,payload)
    assert response.status_code==400,response.text
    _no_side_effects(review_db,storage)


def test_database_failure_after_staging_leaves_no_project_or_files(project_client,review_db,storage,monkeypatch):
    client,issue=project_client
    _seed_file(review_db,storage)
    exported=_export(client,issue)
    from modules.archives.routes import projects
    def boom(*args,**kwargs):raise RuntimeError('synthetic insert failure')
    monkeypatch.setattr(projects,'execute_insert_returning_id',boom)
    with pytest.raises(RuntimeError):_import(client,issue,exported)
    _no_side_effects(review_db,storage)


def test_round_trip_twice_is_stable(project_client,review_db,storage):
    client,issue=project_client
    _seed_file(review_db,storage)
    first=_import(client,issue,_export(client,issue)).json()['id']
    second=client.get(f'/projects/{first}/export',headers=issue(['read:jobs']))
    assert second.status_code==200,second.text
    with zipfile.ZipFile(io.BytesIO(second.content)) as package:
        assert json.loads(package.read('project.json'))['archives'][0]['display']['print_name']=='Own archive'
        assert package.read('files/000.3mf')==b'owned-project-archive-bytes'


def test_archive_linked_to_education_by_rule_exports_without_bytes(project_client,review_db,storage,monkeypatch):
    """Linked-submission/upload-path history (not only the direct marker) never leaves as generic bytes."""
    from modules.archives.routes import projects
    client,issue=project_client
    _seed_file(review_db,storage)
    monkeypatch.setattr(projects,'generic_archive_audience_sql',lambda:'1=0')
    with zipfile.ZipFile(io.BytesIO(_export(client,issue))) as package:
        assert package.namelist()==['project.json']
        archive=json.loads(package.read('project.json'))['archives'][0]
        assert archive['protection']=='education' and archive['file'] is None


def test_shared_file_reprints_stop_at_package_budget_before_reading_more(project_client,review_db,storage,monkeypatch):
    from modules.archives import project_zip_io
    client,issue=project_client
    source=_seed_file(review_db,storage,body=b'x'*1000)
    for identifier in range(300,305):
        review_db.execute(text("INSERT INTO print_archives(id,print_name,status,org_id,ownership_state,ownership_source,project_id,file_path) "
                               "VALUES(:id,'Reprint','completed',1,'known','synthetic_owner',101,:p)"),{'id':identifier,'p':str(source)})
    review_db.commit()
    monkeypatch.setattr(project_zip_io,'MAX_TOTAL_BYTES',2500)
    reads=[];original=project_zip_io.os.fdopen
    monkeypatch.setattr(project_zip_io.os,'fdopen',lambda *a,**k:(reads.append(1),original(*a,**k))[1])
    response=client.get('/projects/101/export',headers=issue(['read:jobs']))
    assert response.status_code==422 and '512 MiB' in response.json()['detail']
    assert len(reads)==2


def test_export_refuses_package_import_would_reject(project_client,review_db,storage,monkeypatch):
    from modules.archives import project_zip_io
    client,issue=project_client
    _seed_file(review_db,storage)
    monkeypatch.setattr(project_zip_io,'MAX_COMPRESSED_BYTES',100)
    response=client.get('/projects/101/export',headers=issue(['read:jobs']))
    assert response.status_code==422 and '256 MiB' in response.json()['detail']


def test_large_non_ascii_metadata_round_trips_or_fails_at_export(project_client,review_db,storage,monkeypatch):
    from modules.archives import project_zip_io
    client,issue=project_client
    review_db.execute(text("UPDATE print_archives SET project_id=101,notes=:n WHERE id=201"),{'n':'é'*9000});review_db.commit()
    exported=_export(client,issue)
    assert len(zipfile.ZipFile(io.BytesIO(exported)).read('project.json'))<30000
    assert _import(client,issue,exported).status_code==200
    monkeypatch.setattr(project_zip_io,'MAX_METADATA_BYTES',1000)
    response=client.get('/projects/101/export',headers=issue(['read:jobs']))
    assert response.status_code==422 and '1 MiB' in response.json()['detail']


def test_comma_tag_is_refused_and_uppercase_suffix_accepted(project_client,review_db,storage):
    client,issue=project_client
    _seed_file(review_db,storage)
    exported=_export(client,issue)
    def comma(manifest,members):manifest['archives'][0]['display']['tags']=['a,b']
    refused=client.post('/projects/import',headers=issue(['write:jobs']),files={'file':('P.ZIP',_repack(exported,comma),'application/zip')})
    assert refused.status_code==400 and 'commas' in refused.json()['detail']
    _no_side_effects(review_db,storage)
    accepted=client.post('/projects/import',headers=issue(['write:jobs']),files={'file':('P.ZIP',exported,'application/zip')})
    assert accepted.status_code==200


def test_restored_file_keeps_original_name(project_client,review_db,storage):
    client,issue=project_client
    _seed_file(review_db,storage,name='bracket v2.3mf')
    project=_import(client,issue,_export(client,issue)).json()['id']
    path=review_db.execute(text('SELECT file_path FROM print_archives WHERE project_id=:p'),{'p':project}).scalar_one()
    assert Path(path).name=='000-bracket v2.3mf'


def test_empty_storage_root_setting_falls_back_to_default(monkeypatch):
    from modules.archives import project_zip_io
    monkeypatch.setenv('PRINT_FILE_STORAGE_ROOT','')
    assert project_zip_io.storage_root()==__import__('os').path.realpath('/data/print_files')
    monkeypatch.setenv('PRINT_FILE_STORAGE_ROOT','relative/root')
    with pytest.raises(RuntimeError):project_zip_io.storage_root()
