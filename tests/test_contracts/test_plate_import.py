"""Synthetic backend-only sliced-plate imports through real child isolation."""
import hashlib
import io
import json
import os
from pathlib import Path
import threading
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db  # noqa: F401


def package(*, numbers=(1, 4, 10), machine='Voron 2.4', multi=False, extra=(), bed=220, nozzle=0.4):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        project = {'printer_model': machine, 'nozzle_diameter': [str(nozzle)],
                   'bed_shape': [f'0x0', f'{bed}x0', f'{bed}x{bed}', f'0x{bed}']}
        archive.writestr('Metadata/project_settings.config', json.dumps(project))
        second = '<filament id="2" type="PETG" color="#000000" used_g="2"/>' if multi else ''
        archive.writestr('Metadata/slice_info.config', f'''<config><plate>
            <metadata key="index" value="1"/><metadata key="prediction" value="120"/>
            <filament id="1" type="PLA" color="#ff0000" used_g="3.5" used_m="1"/>{second}
            </plate><plate><metadata key="index" value="4"/></plate><plate><metadata key="index" value="10"/>
            <metadata key="prediction" value="600"/>
            <filament id="1" type="PETG" color="#00ff00" used_g="7"/>
            </plate></config>''')
        for number in numbers:
            archive.writestr(f'Metadata/plate_{number}.gcode', f'; synthetic plate {number}\nG28\n')
        for name, content in extra:
            archive.writestr(name, content)
    return stream.getvalue()


@pytest.fixture
def importer(token_client, tmp_path, monkeypatch, review_db):
    from modules.models_library.routes import plate_imports
    client, issue = token_client
    client.app.include_router(plate_imports.router)
    plate_imports.limiter._storage.reset()
    root = tmp_path / 'payloads'
    root.mkdir()
    monkeypatch.setenv('PRINT_FILE_STORAGE_ROOT', str(root))
    monkeypatch.setenv('ODIN_RUN_DIR', str(tmp_path / 'worker-slots'))
    review_db.execute(text("UPDATE printers SET api_type='moonraker',machine_type='Voron 2.4',"
                           "is_active=1,bed_x_mm=250,bed_y_mm=250,nozzle_diameter=0.4 WHERE id=9"))
    review_db.commit()
    return TestClient(client.app, raise_server_exceptions=False), issue, root


def upload(importer, content=None, *, target=9, filename='Synthetic.3mf', scopes=None):
    client, issue, _root = importer
    return client.post('/print-files/import-plates', headers=issue(scopes or ['write:models']),
                       data={} if target is None else {'target_printer_id': str(target)},
                       files={'file': (filename, package() if content is None else content, 'application/octet-stream')})


def snapshot(db):
    return tuple(db.execute(text(f'SELECT COUNT(*) FROM {table}')).scalar_one()
                 for table in ('models', 'print_files', 'system_config'))


def test_sparse_import_distinct_models_metadata_hashes_and_bindings(importer, review_db):
    before = snapshot(review_db)
    response = upload(importer)
    assert response.status_code == 200, response.text
    body = response.json()
    files = body['files']
    assert body['plate_count'] == 3 and [f['source_plate'] for f in files] == [1, 4, 10]
    assert len({f['model_id'] for f in files}) == 3
    assert len({f['id'] for f in files}) == 3
    assert [f['filename'] for f in files] == [f'Synthetic_plate_{n:09d}.gcode' for n in (1, 4, 10)]
    assert files[0]['filename'].removesuffix('.gcode') not in files[2]['filename']
    assert [f['print_time_seconds'] for f in files] == [120, None, 600]
    assert [f['total_weight_grams'] for f in files] == [3.5, None, 7]
    assert files[1]['filaments'] == []
    from core.print_file_binding import target_printer, job_file_id
    for item in files:
        row = review_db.execute(text('SELECT * FROM print_files WHERE id=:id'), {'id': item['id']}).mappings().one()
        content = f"; synthetic plate {item['source_plate']}\nG28\n".encode()
        assert Path(row['stored_path']).read_bytes() == content
        assert row['file_hash'] == hashlib.sha256(content).hexdigest()
        assert (row['org_id'], row['created_by']) == (1, 3)
        assert (row['bed_x_mm'], row['bed_y_mm'], row['nozzle_diameter']) == (220, 220, 0.4)
        assert row['compatible_api_types'] == 'moonraker'
        assert json.loads(row['compatibility_facts_json'])['machine']['value'] == 'Voron 2.4'
        assert target_printer(review_db, item['id']) == 9
        assert job_file_id(review_db, item['model_id']) == item['id']
        assert review_db.execute(text('SELECT org_id FROM models WHERE id=:id'), {'id': item['model_id']}).scalar_one() == 1
    assert snapshot(review_db) == tuple(n + 3 for n in before)
    assert not list(importer[2].glob('.plate-import-*'))
    assert review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one() == 1


@pytest.mark.parametrize('source', ['Bambu Lab A1', 'BL-P001', 'P2S'])
def test_bambu_sliced_sources_are_refused(importer, review_db, source):
    before = snapshot(review_db)
    response = upload(importer, package(machine=source))
    assert response.status_code == 400, response.text
    assert snapshot(review_db) == before and not list(importer[2].iterdir())


@pytest.mark.parametrize('change', ['bambu_api', 'bambu_machine', 'foreign', 'inactive', 'small_bed', 'wrong_nozzle', 'unknown_bed'])
def test_target_safety_and_org_checks(importer, review_db, change):
    values = {'bambu_api': "api_type='bambu'", 'bambu_machine': "machine_type='A1'",
              'foreign': 'org_id=2', 'inactive': 'is_active=0', 'small_bed': 'bed_x_mm=100',
              'wrong_nozzle': 'nozzle_diameter=0.6', 'unknown_bed': 'bed_x_mm=NULL',
              'unknown_nozzle': 'nozzle_diameter=NULL'}
    if change == 'foreign':
        review_db.execute(text("INSERT INTO printers(id,name,org_id,api_type,is_active) VALUES(19,'Foreign target',2,'moonraker',1)"))
    else:
        review_db.execute(text('UPDATE printers SET ' + values[change] + ' WHERE id=9'))
    review_db.commit()
    before = snapshot(review_db)
    response = upload(importer, target=19 if change == 'foreign' else 9)
    assert response.status_code == (404 if change == 'foreign' else 400), response.text
    assert snapshot(review_db) == before and not list(importer[2].iterdir())


def test_explicit_target_required_for_unknown_machine(importer):
    response = upload(importer, package(machine='unknown'), target=None)
    assert response.status_code == 422
    assert not list(importer[2].iterdir())


def test_multimaterial_refused_without_validated_target_capability(importer, review_db):
    before = snapshot(review_db)
    assert upload(importer, package(multi=True)).status_code == 400
    assert snapshot(review_db) == before and not list(importer[2].iterdir())


@pytest.mark.parametrize('filename', ['odin-' + 'a' * 32 + '.3mf', 'odin-dispatch-fixture.3mf', '../escape.3mf', 'file.gcode'])
def test_reserved_or_unsafe_upload_names_refused(importer, filename):
    assert upload(importer, filename=filename).status_code == 400
    assert not list(importer[2].iterdir())


@pytest.mark.parametrize('scopes', [['read:models'], ['write:jobs']])
def test_scope_refusal_leaves_database_and_files_unchanged(importer, review_db, scopes):
    before = snapshot(review_db)
    assert upload(importer, scopes=scopes).status_code == 403
    assert snapshot(review_db) == before and not list(importer[2].iterdir())


@pytest.mark.parametrize('extra', [[('../escape', b'x')], [('Metadata/plate_01.gcode', b'G28')],
                                  [('Metadata/plate_1.gcode', b'duplicate')], [('Metadata/plate_0.gcode', b'G28')]])
def test_hostile_archive_refused_without_files(importer, review_db, extra):
    before = snapshot(review_db)
    assert upload(importer, package(extra=extra)).status_code == 400
    assert snapshot(review_db) == before and not list(importer[2].iterdir())


def test_empty_and_excess_plate_counts_refused(importer):
    for numbers in ((), tuple(range(1, 34))):
        assert upload(importer, package(numbers=numbers)).status_code == 400
        assert not list(importer[2].iterdir())


@pytest.mark.parametrize('failure', ['insert', 'binding', 'move'])
def test_known_batch_failure_rolls_back_every_row_and_file(importer, review_db, monkeypatch, failure):
    from modules.models_library.routes import plate_imports
    before = snapshot(review_db)
    def refused(*_args, **_kwargs):
        raise OSError('Synthetic failure')
    if failure == 'insert':
        real = plate_imports.execute_insert_returning_id
        seen = []
        def fail_later(*args, **kwargs):
            seen.append(1)
            if len(seen) == 3:
                refused()
            return real(*args, **kwargs)
        monkeypatch.setattr(plate_imports, 'execute_insert_returning_id', fail_later)
    elif failure == 'binding':
        monkeypatch.setattr(plate_imports, 'bind_file', refused)
    else:
        real = os.replace
        seen = []
        def fail_second(*args):
            seen.append(1)
            if len(seen) == 2:
                refused()
            return real(*args)
        monkeypatch.setattr(plate_imports.os, 'replace', fail_second)
    response = upload(importer)
    assert response.status_code == 500, response.text
    assert snapshot(review_db) == before and not list(importer[2].iterdir())


def test_hashing_and_extraction_finish_off_loop_before_upload_lock(importer, monkeypatch):
    from modules.models_library import plate_import, mesh_isolation
    from modules.models_library.routes import plate_imports
    real_prepare = plate_import.prepare_staged
    real_fresh = plate_imports._fresh_upload_actor
    real_isolated = mesh_isolation.import_plates_isolated
    real_run = mesh_isolation.run_admitted
    seen = []

    async def isolated(*args, **kwargs):
        seen.append(('loop', threading.get_ident()))
        return await real_isolated(*args, **kwargs)

    def run(*args, **kwargs):
        assert [item[0] for item in seen] == ['loop']
        seen.append(('extract', threading.get_ident()))
        return real_run(*args, **kwargs)

    def prepare(*args):
        assert [item[0] for item in seen] == ['loop', 'extract']
        seen.append(('prepare', threading.get_ident()))
        result = real_prepare(*args)
        assert all(item.get('sha256') for item in result)
        return result

    async def fresh(*args):
        assert [item[0] for item in seen] == ['loop', 'extract', 'prepare']
        seen.append(('lock', threading.get_ident()))
        return await real_fresh(*args)

    monkeypatch.setattr(mesh_isolation, 'import_plates_isolated', isolated)
    monkeypatch.setattr(mesh_isolation, 'run_admitted', run)
    monkeypatch.setattr(plate_import, 'prepare_staged', prepare)
    monkeypatch.setattr(plate_imports, '_fresh_upload_actor', fresh)
    assert upload(importer).status_code == 200
    assert [item[0] for item in seen] == ['loop', 'extract', 'prepare', 'lock']
    assert all(thread_id != seen[0][1] for _stage, thread_id in seen[1:])


def test_uncertain_commit_preserves_final_payloads(importer, review_db, monkeypatch):
    from modules.models_library.routes import plate_imports
    before = snapshot(review_db)
    real_fresh = plate_imports._fresh_upload_actor
    async def fresh(*args):
        actor = await real_fresh(*args)
        real_commit = args[0].commit
        def lost_acknowledgement():
            real_commit()
            raise OSError('Synthetic lost acknowledgement after commit')
        monkeypatch.setattr(args[0], 'commit', lost_acknowledgement)
        return actor
    monkeypatch.setattr(plate_imports, '_fresh_upload_actor', fresh)
    response = upload(importer)
    assert response.status_code == 500 and 'uncertain' in response.text
    assert snapshot(review_db) == tuple(n + 3 for n in before)
    files = list(importer[2].glob('*.gcode'))
    assert len(files) == 3 and all(path.stat().st_size > 0 for path in files)
    assert not list(importer[2].glob('.plate-import-*'))


def test_changed_actor_after_staging_is_refused(importer, review_db, monkeypatch):
    from modules.models_library.routes import plate_imports
    real_fresh = plate_imports._fresh_upload_actor
    before = snapshot(review_db)
    async def fresh(db, actor):
        db.execute(text('UPDATE users SET is_active=0 WHERE id=:id'), {'id': actor['id']})
        return await real_fresh(db, actor)
    monkeypatch.setattr(plate_imports, '_fresh_upload_actor', fresh)
    response = upload(importer)
    assert response.status_code == 403, response.text
    assert snapshot(review_db) == before and not list(importer[2].iterdir())


def test_stream_and_total_caps_are_enforced(tmp_path, monkeypatch):
    from modules.models_library import plate_import
    archive = tmp_path / 'source.3mf'
    archive.write_bytes(package())
    destination = tmp_path / 'stage'
    destination.mkdir()
    monkeypatch.setattr(plate_import, 'MAX_PLATE_BYTES', 5)
    with pytest.raises(ValueError, match='size limit'):
        plate_import.extract_to(archive, destination)
    monkeypatch.setattr(plate_import, 'MAX_PLATE_BYTES', 100)
    monkeypatch.setattr(plate_import, 'MAX_EXTRACTED_BYTES', 30)
    with pytest.raises(ValueError, match='size limit'):
        plate_import.extract_to(archive, destination)
    assert not list(destination.iterdir())


def test_worker_failure_never_enters_writer_transaction(importer, monkeypatch):
    from modules.models_library.routes import plate_imports
    from modules.models_library import mesh_isolation
    seen = []
    real_import = mesh_isolation.import_plates_isolated
    async def timeout(path, destination):
        return await real_import(path, destination, timeout=0.1)
    async def no_lock(*args):
        seen.append(1)
        raise AssertionError('Failed child must not acquire upload lock')
    monkeypatch.setenv('ODIN_MESH_WORKER_TEST_SLEEP', '2')
    monkeypatch.setattr(mesh_isolation, 'import_plates_isolated', timeout)
    monkeypatch.setattr(plate_imports, '_fresh_upload_actor', no_lock)
    response = upload(importer)
    assert response.status_code == 400, response.text
    assert seen == [] and not list(importer[2].iterdir())
    assert mesh_isolation.running_workers() == 0


def test_worker_manifest_rejects_hostile_values():
    from modules.models_library.plate_import import validate_result
    for payload in ({'status': 'ok', 'plates': []}, {'status': 'ok', 'plates': [{'number': '../escape'}]},
                    {'status': 'ok', 'plates': 'not a list'}, {'status': 'error'}):
        with pytest.raises(ValueError):
            validate_result(payload)


def test_unparsed_large_geometry_not_subject_to_metadata_cap(tmp_path, monkeypatch):
    from modules.models_library import plate_import
    archive = tmp_path / 'source.3mf'
    archive.write_bytes(package(extra=[('3D/3dmodel.model', b'x' * 2048)]))
    destination = tmp_path / 'stage'
    destination.mkdir()
    monkeypatch.setattr(plate_import, 'MAX_METADATA_BYTES', 1024)
    assert len(plate_import.extract_to(archive, destination)['plates']) == 3


def test_case_insensitive_metadata_cap(tmp_path, monkeypatch):
    from modules.models_library import plate_import
    archive = tmp_path / 'source.3mf'
    archive.write_bytes(package(extra=[('Metadata/EXTRA.CONFIG', b'x' * 2048)]))
    destination = tmp_path / 'stage'
    destination.mkdir()
    monkeypatch.setattr(plate_import, 'MAX_METADATA_BYTES', 1024)
    with pytest.raises(ValueError, match='metadata exceeds'):
        plate_import.extract_to(archive, destination)
    assert not list(destination.iterdir())


@pytest.mark.parametrize('key,value', [('machine_model', 'Creality K1'), ('nozzle_diameter', '0.6')])
def test_conflicting_plate_facts_cannot_override_parent(importer, key, value):
    original = package()
    stream = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original)) as source, zipfile.ZipFile(stream, 'w') as output:
        for name in source.namelist():
            content = source.read(name)
            if name == 'Metadata/slice_info.config':
                content = content.replace(b'<metadata key="index" value="1"/>',
                    f'<metadata key="index" value="1"/><metadata key="{key}" value="{value}"/>'.encode())
            output.writestr(name, content)
    response = upload(importer, stream.getvalue())
    assert response.status_code == 400, response.text
    assert not list(importer[2].iterdir())


def test_upload_cap_cleans_staging_before_worker(importer, monkeypatch):
    from modules.models_library import plate_import
    monkeypatch.setattr(plate_import, 'MAX_UPLOAD_BYTES', 8)
    assert upload(importer).status_code == 413
    assert not list(importer[2].iterdir())


def test_stream_rejects_observed_bytes_beyond_declared_size(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from modules.models_library import plate_import, print_file_meta
    from core import print_payload, zip_bounds
    entry = SimpleNamespace(filename='Metadata/plate_1.gcode', external_attr=0, flag_bits=0,
                            file_size=5, is_dir=lambda: False)
    class LyingPackage:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
        def infolist(self):
            return [entry]
        def namelist(self):
            return [entry.filename, 'Metadata/slice_info.config']
        def getinfo(self, _name):
            return entry
        def open(self, _member):
            if _member == 'Metadata/slice_info.config':
                return io.BytesIO(b'<config><plate><metadata key="index" value="1"/></plate></config>')
            return io.BytesIO(b'G28\nextra bytes')
    facts = {key: print_file_meta._missing_fact() for key in ('api_types', 'machine', 'bed', 'nozzle', 'materials')}
    monkeypatch.setattr(print_payload, 'bambu_plate_numbers', lambda _path: [1])
    monkeypatch.setattr(zip_bounds, 'open_bounded', lambda _path: LyingPackage())
    monkeypatch.setattr(print_file_meta, 'extract_print_file_meta', lambda *_args: {
        'bed_x_mm': None, 'bed_y_mm': None, 'compatible_api_types': '', 'safety_facts': facts})
    with pytest.raises(ValueError, match='stream exceeded'):
        plate_import.extract_to('unused', tmp_path)
    assert (tmp_path / '000000001.gcode').read_bytes() == b''


@pytest.mark.parametrize('shared', [False, True])
def test_shared_target_matches_existing_upload_visibility(importer, review_db, shared):
    review_db.execute(text('''INSERT INTO printers(id,name,org_id,shared,api_type,machine_type,is_active,
        bed_x_mm,bed_y_mm,nozzle_diameter) VALUES(19,'Shared test target',2,:shared,'moonraker','Voron 2.4',1,250,250,0.4)'''),
        {'shared': shared})
    review_db.commit()
    before = snapshot(review_db)
    response = upload(importer, target=19)
    assert response.status_code == (200 if shared else 404), response.text
    if not shared:
        assert snapshot(review_db) == before and not list(importer[2].iterdir())
        return
    from core.print_file_binding import target_printer
    for item in response.json()['files']:
        assert target_printer(review_db, item['id']) == 19
        assert review_db.execute(text('SELECT org_id,created_by FROM print_files WHERE id=:id'),
                                 {'id': item['id']}).one() == (1, 3)
        assert review_db.execute(text('SELECT org_id FROM models WHERE id=:id'),
                                 {'id': item['model_id']}).scalar_one() == 1


@pytest.mark.parametrize('member,content', [
    ('Metadata/slice_info.config', '<config><plate><metadata key="index" value="1"/><metadata key="machine_model" value="Bambu Lab A1"/><metadata key="machine_model" value="Voron 2.4"/></plate></config>'),
    ('Metadata/slice_info.config', '<config><plate><metadata key="index" value="1"/><metadata key="nozzle_diameter" value="0.6"/><metadata key="nozzle_diameter" value="0.4"/></plate></config>'),
    ('Metadata/project_settings.config', '{"printer_model":"Bambu Lab A1","printer_model":"Voron 2.4"}'),
    ('Metadata/plate_1.json', '{"nozzle_diameter":0.6,"nozzle_diameter":0.4}'),
    ('Metadata/project_settings.config', '{"printer_model":"Voron 2.4","machine_model":"Bambu Lab A1"}'),
    ('Metadata/slice_info.config', '<config><plate><metadata key="index" value="1"/><metadata key="printer_model" value="Voron 2.4"/><metadata key="machine_model" value="Bambu Lab A1"/></plate></config>'),
    ('Metadata/project_settings.config', '{"nozzle_diameter":["0.6"],"nozzle_diameters":["0.4"]}'),
    ('Metadata/SLICE_INFO.CONFIG', '<config><plate><metadata key="index" value="1"/><metadata key="machine_model" value="Bambu Lab A1"/><metadata key="machine_model" value="Voron 2.4"/></plate></config>'),
])
def test_duplicate_or_conflicting_declarations_cannot_hide_machine_or_nozzle(importer, member, content):
    stream = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(package())) as source, zipfile.ZipFile(stream, 'w') as output:
        for name in source.namelist():
            if name.lower() != member.lower():
                output.writestr(name, source.read(name))
        output.writestr(member, content)
    response = upload(importer, stream.getvalue())
    assert response.status_code == 400, response.text
    assert not list(importer[2].iterdir())
