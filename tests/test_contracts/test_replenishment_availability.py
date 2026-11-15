"""Legitimate deleted sources isolate planning; malformed or foreign evidence never does."""
import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import text, update

from modules.orders import batch_production as batches, replenishment
from tests.test_contracts.test_replenishment_list import (
    batch, floor, review_db, shop,  # noqa: F401
    assert_no_planning, listed, numbers, plate, queue, remap,
)


def two_products(floor):
    """Two independent source chains with real typed recipe/print records."""
    first = plate(floor)
    numbers(floor)
    numbers(floor, 104)
    remap(floor, 104, 'V4')
    floor['state']['levels']['V4'] = '2'
    db = floor['db']
    db.execute(text("INSERT INTO printers(id,name,org_id,api_type) VALUES(401,'Other printer',1,'moonraker')"))
    db.execute(text("INSERT INTO models(id,name,org_id) VALUES(401,'Other model',1)"))
    db.execute(text("INSERT INTO print_files(id,filename,stored_path,org_id,model_id,print_time_seconds) "
                    "VALUES(401,'plate.gcode',:path,1,401,3600)"), {'path': str(floor['plate'])})
    db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,model_id,print_file_id,printer_id,quantity) "
                    "VALUES(401,'Other reference','cancelled',1,401,401,401,1)"))
    from core.economic_source_creation import _create_attempt
    observation = _create_attempt(db, {'printer_id': 401, 'scheduled_job_id': 401,
                                      'status': 'running', 'started_at': datetime.now(timezone.utc)})
    db.execute(text("UPDATE print_jobs SET status='completed',ended_at=CURRENT_TIMESTAMP WHERE id=:id"), {'id': observation.id})
    uid = db.execute(text('SELECT economic_birth_nonce FROM print_jobs WHERE id=:id'), {'id': observation.id}).scalar_one()
    definition = json.loads(db.execute(text('SELECT definition FROM production_recipes')).scalar_one())
    definition.update(name='Other plate', reference_job_id=401, reference_observation_uid=uid)
    definition['payload'].update(model_id=401, print_file_id=401, printer_id=401)
    definition['outputs'] = [{**definition['outputs'][0], 'product_id': 104, 'product_name': 'Other product'}]
    second = 'e' * 32
    db.execute(batches.recipes.insert().values(id=second, owner=1, definition=batches.packed(definition)))
    db.commit()
    return first, second


def rows(floor):
    return {r['product_id']: r for r in listed(floor)['rows']}


def assert_scoped(floor, *, reason=None):
    result = listed(floor)
    entries = {r['product_id']: r for r in result['rows']}
    blocked, intact = entries[101], entries[104]
    assert blocked['status'] == 'blocked' and 'no longer exists' in blocked['reason']
    if reason:
        assert reason in blocked['reason']
    assert all(field not in blocked for field in replenishment.PLANNING_FIELDS)
    assert blocked['on_hand']['quantity'] == 2 and blocked['target'] == 10
    assert intact['on_the_way'] == {'pieces': 0, 'job_ids': [], 'awaiting_stock': 0}
    assert (intact['shortfall'], intact['plates_needed'], intact['rank']) == (8, 2, 1)
    assert intact['recipe']['id'] == 'e' * 32
    assert result['print_next_count'] == 1
    return result


@pytest.mark.parametrize('model', [None, 501])
def test_ad_hoc_and_unrelated_jobs_leave_every_product_intact(floor, model):
    two_products(floor)
    before = rows(floor)
    db = floor['db']
    db.execute(text("INSERT INTO models(id,name,org_id) VALUES(501,'Unrelated',1)"))
    # More than the scan limit must not crowd out recipe work, even without a model.
    db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,model_id) "
                    "VALUES(:id,'Ad hoc','pending',1,:model)"),
               [{'id': 1000 + i, 'model': model} for i in range(replenishment.LIMIT + 1)])
    db.commit()
    after = rows(floor)
    for snapshot in (before, after):
        for entry in snapshot.values():
            entry['on_hand']['age_seconds'] = 0
    assert after == before


@pytest.mark.parametrize('kind', ['physical print', 'printer', 'model', 'file', 'reference job'])
def test_stale_recipe_blocks_only_its_outputs_and_other_products_can_queue(floor, kind):
    first, second = two_products(floor)
    db = floor['db']
    if kind == 'physical print':
        db.execute(text('DELETE FROM print_jobs WHERE scheduled_job_id=301'))
    elif kind == 'file':
        # The file deleter refuses jobs that still reference it. An outside recipe
        # or a retained recipe after its jobs are removed can outlive the file.
        db.execute(text('UPDATE jobs SET print_file_id=NULL WHERE id IN (301,302)'))
        db.execute(text('DELETE FROM print_files WHERE id=301'))
    elif kind == 'reference job':
        # Accepted jobs cannot be removed; an unaccepted reference job can.
        db.execute(text('DELETE FROM production_acceptances WHERE job_uid=(SELECT economic_uid FROM jobs WHERE id=301)'))
        db.execute(text('DELETE FROM jobs WHERE id=301'))
    else:
        table = {'printer': 'printers', 'model': 'models'}[kind]
        if kind == 'printer':
            # print_jobs has a NO ACTION printer FK; remove observations first.
            db.execute(text('DELETE FROM print_jobs WHERE printer_id=301'))
        db.execute(text(f'DELETE FROM {table} WHERE id=301'))
    db.commit()
    assert_scoped(floor)
    answer = queue(floor, [{'product_id': 101, 'recipe_id': first['id'], 'plates': 1},
                           {'product_id': 104, 'recipe_id': second, 'plates': 2}])
    assert answer.status_code == 200, answer.text
    assert [item['product_id'] for item in answer.json()['skipped']] == [101]
    assert [(item['product_id'], item['plates'], item['held']) for item in answer.json()['queued']] == [(104, 2, True)]


@pytest.mark.parametrize('reference', ['physical_uid', 'print_job_id'])
def test_stale_receipt_blocks_only_its_own_typed_outputs(floor, reference):
    two_products(floor)
    db = floor['db']
    # Re-open a real receipt's prepared stock addition.
    receipt_id, raw = db.execute(
        text('SELECT a.id,a.definition FROM production_acceptances a JOIN jobs j ON a.job_uid=j.economic_uid WHERE j.id=302')).one()
    detail = json.loads(raw)
    pid = db.execute(text('SELECT id FROM print_jobs WHERE scheduled_job_id=302')).scalar_one()
    if reference == 'print_job_id':
        detail['print_job_id'] = pid
        db.execute(update(batches.receipts).where(batches.receipts.c.id == receipt_id).values(definition=batches.packed(detail)))
    db.execute(update(batches.additions).where(batches.additions.c.receipt_id == receipt_id).values(state='prepared'))
    db.execute(text('DELETE FROM print_jobs WHERE id=:id'), {'id': pid})
    db.commit()
    assert_scoped(floor, reason='A stock receipt for this product refers to a print that no longer exists. Review it in Finishing.')


@pytest.mark.parametrize('damage', ['foreign_output', 'malformed_output', 'foreign_file', 'shadow_id'])
def test_stale_reference_never_hides_foreign_or_malformed_data_in_the_same_record(floor, damage):
    first, _ = two_products(floor)
    db = floor['db']
    raw = db.execute(text('SELECT definition FROM production_recipes WHERE id=:id'), {'id': first['id']}).scalar_one()
    definition = json.loads(raw)
    definition['reference_observation_uid'] = 'removed-print'
    if damage == 'foreign_output':
        definition['outputs'][0]['product_id'] = 102
    elif damage == 'malformed_output':
        definition['outputs'][0]['product_id'] = '101'
    elif damage == 'foreign_file':
        db.execute(text('UPDATE print_files SET org_id=2 WHERE id=301'))
    else:
        definition['id'] = 'e' * 32
    db.execute(update(batches.recipes).where(batches.recipes.c.id == first['id']).values(definition=batches.packed(definition)))
    db.commit()
    assert_no_planning(listed(floor))


@pytest.mark.parametrize('field', ['id', 'recipe_id'])
def test_receipt_shadow_columns_block_every_product(floor, field):
    two_products(floor)
    db = floor['db']
    receipt_id, raw = db.execute(text('SELECT id,definition FROM production_acceptances LIMIT 1')).one()
    detail = json.loads(raw)
    detail[field] = 'f' * 32
    db.execute(update(batches.receipts).where(batches.receipts.c.id == receipt_id).values(definition=batches.packed(detail)))
    db.execute(update(batches.additions).where(batches.additions.c.receipt_id == receipt_id).values(state='prepared'))
    db.commit()
    assert_no_planning(listed(floor))


def test_a_stale_record_cannot_hide_a_malformed_later_record(floor):
    first, second = two_products(floor)
    db = floor['db']
    db.execute(text('DELETE FROM print_jobs WHERE scheduled_job_id=301'))
    db.execute(update(batches.recipes).where(batches.recipes.c.id == second).values(definition='{'))
    db.commit()
    assert_no_planning(listed(floor))
    assert queue(floor, [{'product_id': 101, 'recipe_id': first['id'], 'plates': 1}]).json()['queued'] == []


def test_printer_missing_while_observations_survive_is_malformed(floor):
    two_products(floor)
    db = floor['db']
    # This state is impossible through deletion with foreign keys enabled.
    db.commit()
    db.execute(text('PRAGMA foreign_keys=OFF'))
    db.execute(text('DELETE FROM printers WHERE id=301'))
    db.commit()
    db.execute(text('PRAGMA foreign_keys=ON'))
    assert_no_planning(listed(floor))


def test_stale_component_without_its_own_product_outputs_blocks_the_whole_list(floor):
    from tests.test_contracts.test_finishing_workflow import bom, component, finish_body
    preview = bom(floor['batch'])
    inspected = component(floor['batch']).json()
    answer = floor['client'].post('/products/production/finishing', json=finish_body(floor['batch'], inspected, preview))
    assert answer.status_code == 200, answer.text
    numbers(floor)
    numbers(floor, 104)
    assert rows(floor)[101]['on_the_way']['awaiting_stock'] == 2
    floor['db'].execute(text('DELETE FROM print_jobs WHERE scheduled_job_id=:id'), {'id': inspected['job_id']})
    floor['db'].commit()
    result = listed(floor)
    assert_no_planning(result)
    assert all('Finishing receipt scan' in r['reason'] for r in result['rows'])


def test_missing_model_retained_by_component_history_is_malformed_even_without_pending_stock(floor):
    from tests.test_contracts.test_finishing_workflow import bom, component
    from tests.test_contracts.test_replenishment_list import make_recipe
    make_recipe(floor['batch'])
    bom(floor['batch'])
    assert component(floor['batch']).status_code == 200
    numbers(floor)
    numbers(floor, 104)
    floor['db'].execute(text('DELETE FROM models WHERE id=301'))
    floor['db'].commit()
    result = listed(floor)
    assert_no_planning(result)
    assert all('Model ownership scan' in r['reason'] for r in result['rows'])
