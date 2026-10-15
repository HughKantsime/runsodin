"""Certification group C — Model Library (F05) behavior evidence.

Real upload/models/profile routers through real hashed API-token authentication and
the real bootstrapped schema. The 3MF is assembled from the captured Bambu Studio
02.05 slice output in tests/fixtures/telemetry (real slice_info layout and plate
G-code), extended to three plates, two materials, a thumbnail and a wipe tower.
"""
import base64
import hashlib
import io
import json
import subprocess
import zipfile
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / 'tests/fixtures/telemetry/a1-petg-source-sanitized.3mf'
THUMB = cv2.imencode('.png', np.full((16, 16, 3), (30, 120, 200), dtype=np.uint8))[1].tobytes()

SLICE_INFO = '''<?xml version="1.0" encoding="UTF-8"?>
<config>
  <header>
    <header_item key="X-BBL-Client-Type" value="slicer"/>
    <header_item key="X-BBL-Client-Version" value="02.05.00.66"/>
  </header>
  <plate>
    <metadata key="index" value="1"/>
    <metadata key="printer_model_id" value="N2S"/>
    <metadata key="nozzle_diameters" value="0.4"/>
    <metadata key="prediction" value="5430"/>
    <metadata key="weight" value="41.37"/>
    <metadata key="support_used" value="false"/>
    <object identify_id="126" name="Cert bracket" skipped="false" />
    <filament id="1" tray_info_idx="GFL99" type="PLA" color="#FF0000" used_m="9.81" used_g="29.25" used_for_object="true" used_for_support="false" group_id="0" nozzle_diameter="0.40" volume_type="Standard"/>
    <filament id="2" tray_info_idx="GFG99" type="PETG" color="#000000" used_m="3.88" used_g="12.12" used_for_object="true" used_for_support="false" group_id="0" nozzle_diameter="0.40" volume_type="Standard"/>
    <layer_filament_lists>
      <layer_filament_list filament_list="0 1" layer_ranges="0 119" />
    </layer_filament_lists>
  </plate>
</config>'''

PLATE_JSON = {'bed_type': 'textured_plate', 'nozzle_diameter': 0.4,
              'bbox_objects': [{'name': 'Cert bracket', 'layer_height': 0.2}, {'name': 'Cert bracket', 'layer_height': 0.2},
                               {'name': 'Cert clip', 'layer_height': 0.2}, {'name': 'wipe_tower', 'layer_height': 0.2}]}


def sliced_3mf(plates=3, project='Cert bracket'):
    with zipfile.ZipFile(SOURCE) as source:
        gcode = source.read('Metadata/plate_1.gcode')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('3D/3dmodel.model', f'<?xml version="1.0"?><model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><metadata name="Title">{project}</metadata><resources/><build/></model>')
        archive.writestr('Metadata/slice_info.config', SLICE_INFO)
        archive.writestr('Metadata/project_settings.config', json.dumps({'printer_model': 'Bambu Lab A1', 'filament_type': ['PLA', 'PETG']}))
        archive.writestr('Metadata/plate_1.json', json.dumps(PLATE_JSON))
        archive.writestr('Metadata/plate_1.png', THUMB)
        for number in range(1, plates + 1):
            archive.writestr(f'Metadata/plate_{number}.gcode', gcode)
    return buffer.getvalue()


@pytest.fixture
def library(token_client, tmp_path, monkeypatch):
    from modules.models_library.routes import print_files, models_crud
    from modules.organizations.services import EducationPolicyService
    from core.registry import registry
    from modules.jobs import register_providers
    client, issue = token_client
    client.app.include_router(print_files.router)
    client.app.include_router(models_crud.router)
    print_files.limiter._storage.reset()
    register_providers(registry)
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    storage = tmp_path / 'cert-c-upload-storage'
    storage.mkdir()
    monkeypatch.setenv('PRINT_FILE_STORAGE_ROOT', str(storage))
    return TestClient(client.app, raise_server_exceptions=False), issue


def _upload(client, headers, content, name='Cert bracket.3mf'):
    response = client.post('/print-files/upload', headers=headers, files={'file': (name, content, 'application/octet-stream')})
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------- F05-5.1-01..03

def test_sliced_3mf_upload_extracts_time_material_grams_printer_and_thumbnail(library, review_db):
    client, issue = library
    content = sliced_3mf()
    body = _upload(client, issue(['write:models']), content)
    assert body['print_time_seconds'] == 5430 and body['print_time_formatted'] == '1h 30m'
    assert body['total_weight_grams'] == pytest.approx(41.37)
    assert [(f['type'], f['used_grams']) for f in body['filaments']] == [('PLA', 29.25), ('PETG', 12.12)]
    assert body['printer_model'] == 'Bambu Lab A1'
    assert base64.b64decode(body['thumbnail_b64']) == THUMB
    stored = review_db.execute(text('SELECT print_time_seconds,total_weight_grams,printer_model,thumbnail_b64,filaments_json,file_hash FROM print_files WHERE id=:id'), {'id': body['id']}).mappings().one()
    assert (stored['print_time_seconds'], stored['printer_model']) == (5430, 'Bambu Lab A1')
    assert stored['thumbnail_b64'] == body['thumbnail_b64'] and stored['file_hash'] == hashlib.sha256(content).hexdigest()
    assert [(f['type'], f['used_grams']) for f in json.loads(stored['filaments_json'])] == [('PLA', 29.25), ('PETG', 12.12)]
    model = client.get(f"/models/{body['model_id']}", headers=issue(['read:models'])).json()
    assert model['build_time_hours'] == pytest.approx(1.51) and model['default_filament_type'] == 'PLA'


def test_upload_returns_object_checklist_with_wipe_tower_unchecked(library):
    client, issue = library
    body = _upload(client, issue(['write:models']), sliced_3mf())
    assert body['objects'] == [
        {'name': 'Cert bracket', 'is_wipe_tower': False, 'checked': True},
        {'name': 'Cert bracket', 'is_wipe_tower': False, 'checked': True},
        {'name': 'Cert clip', 'is_wipe_tower': False, 'checked': True},
        {'name': 'wipe_tower', 'is_wipe_tower': True, 'checked': False}]


def test_capture_wire_fixture_for_upload_ui(library):
    client, issue = library
    body = _upload(client, issue(['write:models']), sliced_3mf())
    fixture = {'scope': 'Owned real POST /print-files/upload response for the cert C 3-plate 3MF', 'upload': body}
    (REPO / 'frontend/src/test-fixtures/cert-C-upload-wire.json').write_text(json.dumps(fixture, indent=2) + '\n')
    assert body['objects'][-1]['checked'] is False


# ---------------------------------------------------------- F05-5.1-04

def test_checked_object_count_patch_persists_quantity_per_bed(library, review_db):
    client, issue = library
    body = _upload(client, issue(['write:models']), sliced_3mf())
    write = issue(['write:models'])
    # Upload.tsx:370-373 sends the checked count after a toggle.
    assert client.patch(f"/models/{body['model_id']}", headers=write, json={'quantity_per_bed': 2}).status_code == 200
    assert review_db.execute(text('SELECT quantity_per_bed FROM models WHERE id=:id'), {'id': body['model_id']}).scalar_one() == 2
    assert client.get(f"/models/{body['model_id']}", headers=issue(['read:models'])).json()['quantity_per_bed'] == 2


def test_upload_persists_default_checked_object_count(library, review_db):
    client, issue = library
    body = _upload(client, issue(['write:models']), sliced_3mf())
    assert sum(o['checked'] for o in body['objects']) == 3
    assert review_db.execute(text('SELECT quantity_per_bed FROM models WHERE id=:id'), {'id': body['model_id']}).scalar_one() == 3


# ---------------------------------------------------------- F05-5.1-05

def test_multi_plate_schedule_options_list_every_plate_and_bind_selected_plate(library, review_db):
    client, issue = library
    body = _upload(client, issue(['write:models']), sliced_3mf(plates=3))
    options = client.get(f"/models/{body['model_id']}/schedule-options", headers=issue(['write:jobs']))
    assert options.status_code == 200, options.text
    assert options.json()['plate_indices'] == [0, 1, 2] and options.json()['print_file_id'] == body['id']
    scheduled = client.post(f"/models/{body['model_id']}/schedule", headers=issue(['write:jobs']),
                            json={'plate_index': 2, 'print_file_id': body['id'], 'payload_sha256': options.json()['payload_sha256']})
    assert scheduled.status_code == 200, scheduled.text
    job = review_db.execute(text('SELECT plate_index,print_file_id,status FROM jobs WHERE id=:id'), {'id': scheduled.json()['job_id']}).one()
    assert tuple(job) == (2, body['id'], 'pending')
    refused = client.post(f"/models/{body['model_id']}/schedule", headers=issue(['write:jobs']), json={'plate_index': 3})
    assert refused.status_code == 400


def test_variants_endpoint_reports_actual_plate_count(library):
    client, issue = library
    body = _upload(client, issue(['write:models']), sliced_3mf(plates=3))
    variants = client.get(f"/models/{body['model_id']}/variants", headers=issue(['read:models'])).json()['variants']
    assert [v['plate_count'] for v in variants] == [3]


def test_variants_endpoint_exposes_plate_count_field(library):
    client, issue = library
    body = _upload(client, issue(['write:models']), sliced_3mf(plates=1))
    variants = client.get(f"/models/{body['model_id']}/variants", headers=issue(['read:models'])).json()['variants']
    assert [(v['id'], v['plate_count'], v['printer_model']) for v in variants] == [(body['id'], 1, 'Bambu Lab A1')]


# ---------------------------------------------------------- F05-5.2-01/02/04

def test_models_have_no_tags_field_to_search():
    from modules.models_library.models import Model
    from modules.models_library.schemas import ModelResponse
    assert 'tags' not in Model.__table__.columns and 'tags' not in ModelResponse.model_fields


def test_favorite_toggle_persists_and_lists(library):
    client, issue = library
    body = _upload(client, issue(['write:models']), sliced_3mf())
    write, read = issue(['write:models']), issue(['read:models'])
    assert client.patch(f"/models/{body['model_id']}", headers=write, json={'is_favorite': True}).json()['is_favorite'] is True
    assert next(m for m in client.get('/models', headers=read).json() if m['id'] == body['model_id'])['is_favorite'] is True
    assert client.patch(f"/models/{body['model_id']}", headers=write, json={'is_favorite': False}).json()['is_favorite'] is False
    assert client.get(f"/models/{body['model_id']}", headers=read).json()['is_favorite'] is False
    assert client.patch(f"/models/{body['model_id']}", headers=read, json={'is_favorite': True}).status_code == 403


def test_capture_wire_fixture_for_models_ui(library):
    client, issue = library
    first = _upload(client, issue(['write:models']), sliced_3mf(project='Cert bracket'), 'Cert bracket.3mf')
    second = _upload(client, issue(['write:models']), b'; printer_model = MK4\nG28\n', 'Cert hinge.gcode')
    write = issue(['write:models'])
    client.patch(f"/models/{second['model_id']}", headers=write, json={'default_filament_type': 'PETG', 'is_favorite': True})
    rows = client.get('/models-with-pricing', headers=issue(['read:models', 'read:jobs'])).json()
    rows = [r for r in rows if r['id'] in (first['model_id'], second['model_id'])]
    for row in rows:
        for key in ('created_at', 'updated_at'):
            if row.get(key):
                row[key] = '2026-10-14T12:00:00'
    (REPO / 'frontend/src/test-fixtures/cert-C-models-wire.json').write_text(json.dumps({'scope': 'Owned real GET /models-with-pricing response (cert C)', 'models': rows}, indent=2) + '\n')
    assert {r['name'] for r in rows} == {'Cert bracket', 'Cert hinge'}


def test_schedule_edit_and_delete_from_model(library, review_db):
    client, issue = library
    body = _upload(client, issue(['write:models']), sliced_3mf())
    model_id = body['model_id']
    scheduled = client.post(f'/models/{model_id}/schedule', headers=issue(['write:jobs']))
    assert scheduled.status_code == 200 and scheduled.json()['status'] == 'pending'
    assert review_db.execute(text('SELECT model_id,status FROM jobs WHERE id=:id'), {'id': scheduled.json()['job_id']}).one() == (model_id, 'pending')
    write = issue(['write:models'])
    assert client.patch(f'/models/{model_id}', headers=write, json={'name': 'Cert bracket v2', 'notes': 'Cert notes', 'cost_per_item': 2.5}).status_code == 200
    detail = client.get(f'/models/{model_id}', headers=issue(['read:models'])).json()
    assert (detail['name'], detail['notes'], detail['cost_per_item']) == ('Cert bracket v2', 'Cert notes', 2.5)
    assert client.delete(f'/models/{model_id}', headers=issue(['read:models'])).status_code == 403
    assert client.delete(f'/models/{model_id}', headers=write).status_code == 204
    assert client.get(f'/models/{model_id}', headers=issue(['read:models'])).status_code == 404


# ---------------------------------------------------------- provenance labels

@pytest.mark.parametrize('tag,previous,path,needle', [
    ('v1.3.0', 'v1.2.0', 'backend', 'model_revisions'),
    ('v1.2.0', 'v1.1.4', 'backend', 'drying_logs'),
    ('v1.0.33', 'v1.0.32', 'backend', 'consumables'),
])
def test_versioned_feature_first_appears_at_advertised_tag(tag, previous, path, needle):
    def present(ref):
        return subprocess.run(['git', 'grep', '-q', '-i', needle, ref, '--', path], cwd=REPO).returncode == 0
    assert present(tag) and not present(previous)


# ---------------------------------------------------------- F05-5.3-01..03

@pytest.fixture
def revisions(library, tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_REVISION_STORAGE_ROOT', str(tmp_path / 'cert-c-revisions'))
    client, issue = library
    body = _upload(client, issue(['write:models']), sliced_3mf())
    return client, issue, body['model_id']


def test_revision_upload_numbers_dates_uploader_and_capture_wire(revisions):
    client, issue, model_id = revisions
    write = issue(['write:models'])
    for note in ('Cert thicker wall', 'Cert new hinge'):
        created = client.post(f'/models/{model_id}/revisions', headers=write, params={'changelog': note},
                              files={'file': ('rev.3mf', sliced_3mf(plates=1), 'application/octet-stream')})
        assert created.status_code == 200, created.text
    rows = client.get(f'/models/{model_id}/revisions', headers=issue(['read:models'])).json()
    assert [(r['revision_number'], r['changelog'], r['uploaded_by_name']) for r in rows][-2:] in (
        [(1, 'Cert thicker wall', 'admin'), (2, 'Cert new hinge', 'admin')], [(2, 'Cert new hinge', 'admin'), (1, 'Cert thicker wall', 'admin')])
    assert all(r['created_at'] for r in rows)
    for row in rows:
        row['created_at'] = '2026-10-14T12:00:00'
        row['file_path'] = None
    (REPO / 'frontend/src/test-fixtures/cert-C-model-revisions-wire.json').write_text(json.dumps({'scope': 'Owned real GET /models/{id}/revisions (cert C)', 'model_id': model_id, 'revisions': rows}, indent=2) + '\n')


def test_revision_changelog_sent_as_ui_form_field_is_persisted(revisions):
    client, issue, model_id = revisions
    created = client.post(f'/models/{model_id}/revisions', headers=issue(['write:models']), data={'changelog': 'Cert UI note'},
                          files={'file': ('rev.3mf', sliced_3mf(plates=1), 'application/octet-stream')})
    assert created.status_code == 200, created.text
    rows = client.get(f'/models/{model_id}/revisions', headers=issue(['read:models'])).json()
    assert 'Cert UI note' in [r['changelog'] for r in rows]


# ------------------------------------------- regression: cert-fix-C upload/revision fixes

def test_variant_upload_records_its_plate_count_without_overwriting_model_quantity(library, review_db):
    client, issue = library
    first = _upload(client, issue(['write:models']), sliced_3mf(plates=3))
    assert client.patch(f"/models/{first['model_id']}", headers=issue(['write:models']), json={'quantity_per_bed': 2}).status_code == 200
    second = _upload(client, issue(['write:models']), sliced_3mf(plates=1), 'Cert bracket.3mf')
    assert second['model_id'] == first['model_id'] and second['is_new_model'] is False
    assert review_db.execute(text('SELECT quantity_per_bed FROM models WHERE id=:id'), {'id': first['model_id']}).scalar_one() == 2
    variants = client.get(f"/models/{first['model_id']}/variants", headers=issue(['read:models'])).json()['variants']
    assert sorted((v['id'], v['plate_count']) for v in variants) == sorted([(first['id'], 3), (second['id'], 1)])


def test_revision_changelog_form_field_wins_over_legacy_query(revisions):
    client, issue, model_id = revisions
    created = client.post(f'/models/{model_id}/revisions', headers=issue(['write:models']), params={'changelog': 'query note'},
                          data={'changelog': 'form note'}, files={'file': ('rev.3mf', sliced_3mf(plates=1), 'application/octet-stream')})
    assert created.status_code == 200, created.text
    fileless = client.post(f'/models/{model_id}/revisions', headers=issue(['write:models']), params={'changelog': 'fileless query note'})
    assert fileless.status_code == 200, fileless.text
    notes = [r['changelog'] for r in client.get(f'/models/{model_id}/revisions', headers=issue(['read:models'])).json()]
    assert 'form note' in notes and 'query note' not in notes and 'fileless query note' in notes


def test_upload_response_reports_the_models_stored_quantity(library):
    """Lets the Upload page skip a redundant PATCH and never overwrite a variant's model (review MEDIUM-8)."""
    client, issue = library
    first = _upload(client, issue(['write:models']), sliced_3mf(plates=3))
    assert (first['is_new_model'], first['quantity_per_bed']) == (True, 3)
    assert client.patch(f"/models/{first['model_id']}", headers=issue(['write:models']), json={'quantity_per_bed': 6}).status_code == 200
    variant = _upload(client, issue(['write:models']), sliced_3mf(plates=1))
    assert (variant['is_new_model'], variant['quantity_per_bed']) == (False, 6)
