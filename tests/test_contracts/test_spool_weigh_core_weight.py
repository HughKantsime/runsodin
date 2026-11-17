"""Weighing must use a recorded core weight, never an assumed one."""
import pytest
from sqlalchemy import text

from tests.test_contracts.test_inventory_claim_acceptance import inventory_client  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401

DETAIL = ('This spool has no empty-spool weight on record. Edit the spool, enter the '
          'weight of the empty spool, then weigh it again.')


def legacy_spool(client, db):
    response = client.post('/api/spools', json={'filament_id': 990, 'initial_weight_g': 1000})
    assert response.status_code == 200
    sid = response.json()['id']
    db.execute(text('UPDATE spools SET spool_weight_g=NULL WHERE id=:id'), {'id': sid})
    db.commit()
    return sid


def test_weigh_without_core_weight_refuses_plainly(inventory_client, review_db):
    sid = legacy_spool(inventory_client, review_db)
    before = inventory_client.get(f'/api/spools/{sid}').json()
    usage = review_db.execute(text('SELECT * FROM spool_usage')).all()
    config = review_db.execute(text('SELECT * FROM system_config')).all()
    audits = review_db.execute(text('SELECT * FROM audit_logs')).all()
    response = inventory_client.post(f'/api/spools/{sid}/weigh', json={'gross_weight_g': 850})
    assert response.status_code == 409
    assert response.json()['detail'] == DETAIL
    assert inventory_client.get(f'/api/spools/{sid}').json() == before
    assert review_db.execute(text('SELECT * FROM spool_usage')).all() == usage
    assert review_db.execute(text('SELECT * FROM system_config')).all() == config
    assert review_db.execute(text('SELECT * FROM audit_logs')).all() == audits


@pytest.mark.parametrize('core', [0, 180, 250])
def test_weigh_after_setting_core_weight_works(inventory_client, review_db, core):
    sid = legacy_spool(inventory_client, review_db)
    assert inventory_client.patch(f'/api/spools/{sid}', json={'spool_weight_g': core}).status_code == 200
    assert inventory_client.get(f'/api/spools/{sid}').json()['remaining_weight_g'] == 1000
    response = inventory_client.post(f'/api/spools/{sid}/weigh', json={'gross_weight_g': 600 + core})
    assert response.status_code == 200, response.text
    assert response.json()['new_weight_g'] == 600
    assert inventory_client.get(f'/api/spools/{sid}').json()['remaining_weight_g'] == 600
    assert review_db.execute(text('SELECT weight_used_g,job_id,print_job_id FROM spool_usage WHERE spool_id=:id'),
                             {'id': sid}).all() == [(400, None, None)]
