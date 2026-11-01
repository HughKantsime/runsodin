"""Explicit machine declarations must survive sliced-plate target checks."""
import io
import json
import zipfile

import pytest
from sqlalchemy import text

from tests.test_contracts.test_plate_import import (  # noqa: F401
    importer, review_db, token_client, package, snapshot, upload,
)

KEYS = ('printer_model', 'printer_model_id', 'machine_model')


def declared_package(location, declarations):
    stream = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(package(numbers=(1,), machine=None))) as source, zipfile.ZipFile(stream, 'w') as output:
        for name in source.namelist():
            content = source.read(name)
            if name == 'Metadata/project_settings.config':
                data = json.loads(content)
                del data['printer_model']
                if location == 'project':
                    data.update(declarations)
                content = json.dumps(data)
            if name == 'Metadata/slice_info.config' and location == 'slice':
                metadata = ''.join(f'<metadata key="{key}" value="{value}"/>' for key, value in declarations.items())
                content = content.replace(b'<metadata key="index" value="1"/>',
                                          b'<metadata key="index" value="1"/>' + metadata.encode())
            output.writestr(name, content)
        if location == 'json':
            output.writestr('Metadata/plate_1.json', json.dumps(declarations))
    return stream.getvalue()


def target_machine(db, machine):
    db.execute(text('UPDATE printers SET machine_type=:machine WHERE id=9'), {'machine': machine})
    db.commit()


@pytest.mark.parametrize('source,target', [
    ('Creality K1', 'Voron 2.4'), ('Voron 2.4', 'Prusa MK4'),
    ('Prusa MK4', 'Prusa MK4+'), ('Voron 2.4', None), ('Voron 2.4', ''), ('Voron 2.4', '   '),
])
def test_explicit_machine_conflicts_refused_without_persistence(importer, review_db, source, target):
    target_machine(review_db, target)
    before = snapshot(review_db)
    response = upload(importer, package(machine=source))
    assert response.status_code == 400, response.text
    assert snapshot(review_db) == before
    assert not list(importer[2].iterdir())


@pytest.mark.parametrize('location', ['project', 'slice', 'json'])
@pytest.mark.parametrize('key', KEYS)
def test_all_machine_declaration_aliases_checked_against_target(importer, review_db, location, key):
    before = snapshot(review_db)
    response = upload(importer, declared_package(location, {key: 'Prusa MK4'}))
    assert response.status_code == 400, response.text
    assert snapshot(review_db) == before
    assert not list(importer[2].iterdir())


@pytest.mark.parametrize('source,target', [('Voron 2.4', ' VORON2.4 '), ('Creality K1', 'K1')])
def test_matching_unknown_and_known_alias_identities_accepted(importer, review_db, source, target):
    target_machine(review_db, target)
    response = upload(importer, package(machine=source))
    assert response.status_code == 200, response.text
    assert response.json()['plate_count'] == 3


@pytest.mark.parametrize('location', ['project', 'slice', 'json'])
def test_conflicting_alias_declarations_refused(importer, review_db, location):
    before = snapshot(review_db)
    response = upload(importer, declared_package(location, {'printer_model': 'Voron 2.4', 'machine_model': 'Prusa MK4'}))
    assert response.status_code == 400, response.text
    assert snapshot(review_db) == before
    assert not list(importer[2].iterdir())


def test_absent_source_identity_still_allows_explicit_target(importer):
    response = upload(importer, declared_package('project', {}))
    assert response.status_code == 200, response.text


def test_plate_json_cannot_override_project_machine(importer, review_db):
    before = snapshot(review_db)
    response = upload(importer, package(extra=[('Metadata/plate_1.json', json.dumps({'machine_model': 'Prusa MK4'}))]))
    assert response.status_code == 400, response.text
    assert snapshot(review_db) == before
    assert not list(importer[2].iterdir())
