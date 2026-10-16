"""Camera/3MF review MEDIUM 2: per-plate summaries parse untrusted plate JSON in the bounded child.

The reviewer's file: a sliced 3MF whose Metadata/plate_2.json is ~60 MB of small objects
(it deflates to a few hundred KB; json.load of it grows to over 4 GB in Python). GET
/models/{id}/plates and plate>0 scheduling parsed it inside the API process.
"""
import io
import json
import zipfile

import pytest
from sqlalchemy import text

from tests.test_contracts.test_cert_C_models import _upload, library, per_plate_3mf, sliced_3mf  # noqa: F401
from tests.test_contracts.test_api_token_delegation_behavior import token_client  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401


def reviewer_bomb_3mf(member_bytes=60 * 2**20):
    """plate_1 normal; plate_2.json about ``member_bytes`` of tiny objects."""
    base = zipfile.ZipFile(io.BytesIO(sliced_3mf(plates=2)))
    item = '{"name":"o","layer_height":0.2},'
    count = member_bytes // len(item)
    bomb = '{"bbox_objects":[' + item * count + '{"name":"last"}]}'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for info in base.infolist():
            if info.filename != 'Metadata/plate_2.json':
                archive.writestr(info, base.read(info.filename))
        archive.writestr('Metadata/plate_2.json', bomb)
    return buffer.getvalue()


def test_reviewer_file_is_refused_at_upload_by_the_child_checking_every_plate(library):
    client, issue = library
    response = client.post('/print-files/upload', headers=issue(['write:models']),
                           files={'file': ('bomb.3mf', reviewer_bomb_3mf(), 'application/octet-stream')})
    assert response.status_code == 400, response.text
    assert 'plate_N.json' in response.json()['detail']


def test_stored_reviewer_file_is_parsed_only_in_the_child(library, review_db, monkeypatch):
    """A package already stored (e.g. before this check) is summarised by the child, which
    refuses the oversized plate JSON; the API process never json.loads it."""
    from modules.models_library import threemf_parser
    from modules.models_library.routes import _models_helpers
    client, issue = library
    body = _upload(client, issue(['write:models']), per_plate_3mf())
    path = review_db.execute(text('SELECT stored_path FROM print_files WHERE id=:id'), {'id': body['id']}).scalar_one()
    with open(path, 'wb') as stored:
        stored.write(reviewer_bomb_3mf())

    def in_process(*_a, **_k):
        raise AssertionError('plate JSON parsed in the API process')
    monkeypatch.setattr(threemf_parser, 'plate_summaries', in_process)
    monkeypatch.setattr(threemf_parser, 'extract_objects_from_plate', in_process)
    with pytest.raises(Exception) as refused:
        _models_helpers._plate_summaries(path)
    assert getattr(refused.value, 'status_code', None) == 400
    assert 'too large' in refused.value.detail


def test_plates_endpoint_and_plate_scheduling_still_work_through_the_child(library, review_db, monkeypatch):
    from modules.models_library import threemf_parser
    client, issue = library
    body = _upload(client, issue(['write:models']), per_plate_3mf())

    def in_process(*_a, **_k):
        raise AssertionError('plate JSON parsed in the API process')
    monkeypatch.setattr(threemf_parser, 'plate_summaries', in_process)
    options = client.get(f"/models/{body['model_id']}/plates", headers=issue(['write:jobs']))
    assert options.status_code == 200, options.text
    plates = options.json()['plates']
    assert [(p['plate_index'], p['sellable_pieces'], p['print_time_seconds']) for p in plates] == [
        (0, 3, 5430), (1, 2, 1800), (2, 1, 900)]
    payload = client.get(f"/models/{body['model_id']}/schedule-options", headers=issue(['write:jobs'])).json()
    scheduled = client.post(f"/models/{body['model_id']}/schedule", headers=issue(['write:jobs']),
                            json={'plate_index': 2, 'print_file_id': body['id'], 'payload_sha256': payload['payload_sha256']})
    assert scheduled.status_code == 200, scheduled.text
    assert scheduled.json()['estimate_basis'] == 'selected_plate'


def test_in_process_plate_json_reads_are_capped_before_json_load():
    from modules.models_library import threemf_parser
    archive = zipfile.ZipFile(io.BytesIO(reviewer_bomb_3mf(member_bytes=5 * 2**20)))
    loads = []
    original = json.load
    try:
        json.load = lambda *a, **k: loads.append(1) or original(*a, **k)
        assert threemf_parser.extract_objects_from_plate(archive, 2) == []
    finally:
        json.load = original
    assert loads == []
    assert threemf_parser.plate_json_too_large(archive)
    assert not threemf_parser.plate_json_too_large(zipfile.ZipFile(io.BytesIO(per_plate_3mf())))
