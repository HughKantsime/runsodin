"""A slicer's material label can't break the model list.

The upload wrote the label as it came. SQLite stored it, and the ORM then raised on every query that
loaded the row, so one upload made GET /models return 500 for everyone who could see that model.
"""
import io
import re
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from core.base import FilamentType, stored_filament_value
from tests.test_contracts.test_cert_C_models import library, review_db, sliced_3mf, token_client  # noqa: F401

SCOPES = ['write:models', 'read:models']
MAPPED = [
    ('PLA', 'PLA'), ('PETG', 'PETG'), ('PLA-CF', 'PLA_CF'), ('PLA-S', 'PLA_SUPPORT'), ('PETG-CF', 'PETG_CF'), ('PA-CF', 'NYLON_CF'),
    ('ABS-GF', 'OTHER'), ('PC-ABS', 'PC_ABS'), ('PLA+', 'PLA'), ('pla-cf', 'PLA_CF'), (' PLA-CF ', 'PLA_CF'), ('PLA-SILK', 'PLA'),
    ('Unknown', 'Unknown'), ('PLA Silk', 'PLA'), ('PETG HF', 'PETG'), ('Some brand new polymer', 'OTHER'), ('', 'PLA'),
]


def _with_label(label):
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(sliced_3mf())) as source, zipfile.ZipFile(out, 'w') as target:
        for item in source.infolist():
            content = source.read(item.filename)
            if item.filename == 'Metadata/slice_info.config':
                content = re.sub(r'type="[^"]*"', f'type="{label}"', content.decode()).encode()
            target.writestr(item, content)
    return out.getvalue()


@pytest.mark.parametrize('label,expected', MAPPED)
def test_an_upload_stores_a_label_the_model_list_can_read(library, review_db, label, expected):
    client, issue = library
    headers = issue(SCOPES)
    response = client.post('/print-files/upload', headers=headers, files={'file': (f'Label {label.strip() or "blank"}.3mf', _with_label(label), 'application/octet-stream')})
    assert response.status_code == 200, response.text
    assert response.json()['filaments'][0]['type'] == label, 'the file keeps what the slicer said'
    model_id = response.json()['model_id']
    review_db.commit()
    assert review_db.execute(text('SELECT default_filament_type FROM models WHERE id=:id'), {'id': model_id}).scalar_one() == expected
    review_db.commit()
    listed = client.get('/models', headers=headers)
    assert listed.status_code == 200, listed.text
    assert [row for row in listed.json() if row['id'] == model_id][0]['default_filament_type'] == expected
    single = client.get(f'/models/{model_id}', headers=headers)
    assert single.status_code == 200 and single.json()['default_filament_type'] == expected


@pytest.mark.parametrize('stored,shown', [('PLA-CF', 'PLA_CF'), ('PLA Silk', 'PLA'), ('Some brand new polymer', 'OTHER')])
def test_a_label_already_stored_reads_as_its_type_and_the_row_is_left_alone(library, review_db, stored, shown):
    """What an earlier release left in a database. Nothing here may rewrite it."""
    client, issue = library
    headers = issue(SCOPES)
    review_db.execute(text("INSERT INTO models(id,name,default_filament_type,org_id) VALUES(901,'Already stored',:label,1)"), {'label': stored})
    review_db.commit()
    listed = client.get('/models', headers=headers)
    assert listed.status_code == 200, listed.text
    assert [row for row in listed.json() if row['id'] == 901][0]['default_filament_type'] == shown
    assert client.get('/models/901', headers=headers).json()['default_filament_type'] == shown
    assert client.patch('/models/901', headers=headers, json={'notes': 'Edited'}).status_code in (200, 405)
    assert client.put('/models/901', headers=headers, json={'notes': 'Edited'}).status_code in (200, 405)
    review_db.commit()
    assert review_db.execute(text('SELECT default_filament_type FROM models WHERE id=901')).scalar_one() == stored


def test_a_null_stays_null(library, review_db):
    client, issue = library
    review_db.execute(text("INSERT INTO models(id,name,default_filament_type,org_id) VALUES(902,'No material',NULL,1)"))
    review_db.commit()
    assert client.get('/models/902', headers=issue(SCOPES)).json()['default_filament_type'] is None


@pytest.mark.parametrize('member', list(FilamentType))
def test_every_type_is_its_own_stored_value(member):
    assert stored_filament_value(member.value) == member.value
