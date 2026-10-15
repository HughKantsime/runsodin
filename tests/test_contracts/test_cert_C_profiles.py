"""Certification group C — slicer/printer profile library (F05-5.4-04..10).

Real profile router through real hashed API tokens; Moonraker adapter is the stubbed
printer boundary (from profile_client)."""
import json
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.test_contracts.test_profile_library_behavior import profile_client, token_client, review_db

REPO = Path(__file__).resolve().parents[2]

BAMBU_SYSTEM = json.dumps({'type': 'filament', 'name': 'Bambu PLA Basic @BBL A1', 'from': 'system', 'inherits': 'Bambu PLA Basic @base',
                           'filament_type': ['PLA'], 'nozzle_temperature': ['220'], 'version': '2.5.0.14'})
BAMBU_USER = json.dumps({'type': 'process', 'name': 'Cert 0.16mm @BBL A1', 'from': 'User', 'inherits': '0.16mm Optimal @BBL A1',
                         'layer_height': '0.16', 'version': '2.5.0.14'})


@pytest.mark.parametrize('content,expected_slicer,expected_category', [
    (BAMBU_SYSTEM, 'bambu_studio', 'filament'),
    (BAMBU_USER, 'orca', 'process'),
])
def test_bambu_studio_json_imports_and_roundtrips(profile_client, review_db, content, expected_slicer, expected_category):
    client, issue, calls, _ = profile_client
    response = client.post('/profiles/import', headers=issue(['write:models']), files={'file': ('cert.json', content, 'application/json')})
    assert response.status_code == 201, response.text
    identifier = response.json()['ids'][0]
    row = review_db.execute(text('SELECT slicer,category,file_format,raw_content FROM printer_profiles WHERE id=:id'), {'id': identifier}).one()
    # A Bambu Studio *user* preset ("from": "User") is indistinguishable from OrcaSlicer and is labelled orca.
    assert tuple(row) == (expected_slicer, expected_category, 'json', content)
    assert client.get(f'/profiles/{identifier}/export', headers=issue(['read:models'])).text == content
    assert calls == []


def test_profile_categories_are_printer_filament_process_not_suffixed_names(profile_client):
    client, issue, _, _ = profile_client
    headers = issue(['write:models'])
    for advertised in ('slicer_profile', 'printer_profile', 'filament_profile'):
        refused = client.post('/profiles', headers=headers, json={'name': 'Cert', 'slicer': 'orca', 'category': advertised, 'raw_content': '{}'})
        assert refused.status_code == 400, advertised
    for actual in ('printer', 'filament', 'process'):
        created = client.post('/profiles', headers=headers, json={'name': f'Cert {actual}', 'slicer': 'orca', 'category': actual, 'raw_content': '{}'})
        assert created.status_code == 201, created.text
        listed = client.get('/profiles', headers=issue(['read:models']), params={'category': actual}).json()
        assert [p['name'] for p in listed['profiles']] == [f'Cert {actual}']


def test_only_klipper_command_profiles_can_be_distributed_to_printers(profile_client, review_db):
    client, issue, calls, _ = profile_client
    headers = issue(['write:models'])
    slicer = client.post('/profiles', headers=headers, json={'name': 'Cert slicer', 'slicer': 'orca', 'category': 'process', 'raw_content': '{}'}).json()['id']
    cfg = client.post('/profiles/import', headers=headers, files={'file': ('printer.cfg', '[printer]\nkinematics: corexy\n', 'text/plain')}).json()['ids'][0]
    apply = issue(['read:models', 'write:printers'])
    for identifier in (slicer, cfg):
        response = client.post(f'/profiles/{identifier}/apply', headers=apply, json={'printer_id': 9})
        assert response.status_code == 400, response.text
    assert calls == []
    applied = client.post('/profiles/101/apply', headers=apply, json={'printer_id': 9})
    assert applied.status_code == 200, applied.text
    assert ('construct', 'synthetic-own.invalid') in calls and any(kind == 'command' for kind, _ in calls)


def test_profile_crud_endpoints_roundtrip(profile_client):
    client, issue, _, _ = profile_client
    write, read = issue(['write:models']), issue(['read:models'])
    created = client.post('/profiles', headers=write, json={'name': 'Cert CRUD', 'slicer': 'prusa', 'category': 'filament', 'raw_content': 'filament_type = PETG\n', 'tags': 'cert,petg'})
    assert created.status_code == 201, created.text
    identifier = created.json()['id']
    assert client.get(f'/profiles/{identifier}', headers=read).json()['tags'] == 'cert,petg'
    assert client.put(f'/profiles/{identifier}', headers=write, json={'name': 'Cert CRUD v2', 'tags': 'cert'}).status_code == 200
    assert (client.get(f'/profiles/{identifier}', headers=read).json()['name'], client.get(f'/profiles/{identifier}', headers=read).json()['tags']) == ('Cert CRUD v2', 'cert')
    assert any(p['id'] == identifier for p in client.get('/profiles', headers=read, params={'search': 'cert'}).json()['profiles'])
    assert client.delete(f'/profiles/{identifier}', headers=write).status_code == 204
    assert client.get(f'/profiles/{identifier}', headers=read).status_code == 404
