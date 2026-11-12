"""Empty-spool weight (spool_weight_g) is editable after receiving, audited, and never rewrites remaining."""
import re
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.test_contracts.test_inventory_claim_acceptance import inventory_client  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401


def _spool(client, **extra):
    response = client.post('/api/spools', json={'filament_id': 990, 'initial_weight_g': 1000, **extra})
    assert response.status_code == 200
    return response.json()['id']


def test_core_weight_edit_keeps_remaining_and_is_audited(inventory_client, review_db):
    sid = _spool(inventory_client, spool_weight_g=250)
    assert inventory_client.patch(f'/api/spools/{sid}', json={'remaining_weight_g': 600}).status_code == 200
    assert inventory_client.patch(f'/api/spools/{sid}', json={'spool_weight_g': 180}).status_code == 200
    row = inventory_client.get(f'/api/spools/{sid}').json()
    assert row['spool_weight_g'] == 180
    assert row['remaining_weight_g'] == 600  # net filament weight is stored; the core only changes weigh conversion
    rows = review_db.execute(text("SELECT details FROM audit_logs WHERE action='spool.core_weight_updated' AND entity_id=:i"),
                             {'i': sid}).fetchall()
    assert len(rows) == 1
    assert '250' in str(rows[0][0]) and '180' in str(rows[0][0])


def test_unchanged_core_weight_is_not_audited(inventory_client, review_db):
    sid = _spool(inventory_client, spool_weight_g=250)
    assert inventory_client.patch(f'/api/spools/{sid}', json={'spool_weight_g': 250}).status_code == 200
    count = review_db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='spool.core_weight_updated'")).scalar()
    assert count == 0


def test_weigh_uses_the_corrected_core_weight(inventory_client):
    sid = _spool(inventory_client, spool_weight_g=250)
    assert inventory_client.patch(f'/api/spools/{sid}', json={'spool_weight_g': 180}).status_code == 200
    response = inventory_client.post(f'/api/spools/{sid}/weigh', json={'gross_weight_g': 780})
    assert response.status_code == 200
    assert response.json()['new_weight_g'] == 600


@pytest.mark.parametrize('body', [{'spool_weight_g': -1}, {'spool_weight_g': 10001}, {'spool_weight_g': None},
                                  {'spool_weight_g': 'abc'}, {'spool_weight_g': 'nan'}])
def test_invalid_core_weight_refuses_and_changes_nothing(inventory_client, review_db, body):
    sid = _spool(inventory_client, spool_weight_g=250)
    assert inventory_client.patch(f'/api/spools/{sid}', json=body).status_code == 422
    assert inventory_client.get(f'/api/spools/{sid}').json()['spool_weight_g'] == 250
    count = review_db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='spool.core_weight_updated'")).scalar()
    assert count == 0


def test_core_weight_edit_needs_the_spool_edit_action():
    from core.rbac_policy import ACTIONS  # noqa: F401
    assert ACTIONS['spools.edit'] == ['admin', 'operator']


def test_unknown_remaining_weight_is_null_on_the_wire_and_survives_other_edits(inventory_client, review_db):
    sid = _spool(inventory_client)
    review_db.execute(text('UPDATE spools SET remaining_weight_g = NULL WHERE id = :i'), {'i': sid})
    review_db.commit()
    assert inventory_client.get(f'/api/spools/{sid}').json()['remaining_weight_g'] is None
    assert inventory_client.patch(f'/api/spools/{sid}', json={'notes': 'unknown stays unknown'}).status_code == 200
    assert inventory_client.get(f'/api/spools/{sid}').json()['remaining_weight_g'] is None
    listed = next(x for x in inventory_client.get('/api/spools').json() if x['id'] == sid)
    assert listed['remaining_weight_g'] is None


def test_queue_check_payload_matches_the_keys_the_ui_reads():
    """The UI type and the server's per-slot item must stay the same set of keys."""
    from modules.printers import filament_check as fc
    root = Path(__file__).resolve().parents[2]
    source = (root / 'frontend/src/api/filamentCheck.ts').read_text()
    body = re.search(r'interface FilamentCheckSlot \{(.*?)\n\}', source, re.S).group(1)
    ui_keys = set(re.findall(r'^\s+(\w+):', body, re.M))
    row = {'filament_type': 'PLA', 'color': 'black', 'assigned_spool_id': 7, 'remaining_weight_g': 40.0, 'tray_observation': None}
    item = fc._judge(2, [dict(source=0, slot=2, grams=50.0, reason=None)], row, 1, 10.0)
    assert set(item) == ui_keys
    assert item['status'] == 'short'
    assert item['message'] == 'Slot 2 (black PLA) has about 40 g left; this print needs about 55 g'
    row['remaining_weight_g'] = None
    unknown = fc._judge(2, [dict(source=0, slot=2, grams=50.0, reason=None)], row, 1, 10.0)
    assert unknown['status'] == 'unknown' and unknown['reason'] == 'no_remaining_weight'


def test_job_queue_row_mounts_the_filament_check_panel():
    root = Path(__file__).resolve().parents[2]
    row = (root / 'frontend/src/components/jobs/JobRow.tsx').read_text()
    assert 'FilamentCheckPanel' in row and "job.status === 'scheduled'" in row
