"""Retained evidence and public stock reasons from the stock-printing review."""
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text, update

from core.models import SystemConfig
from modules.orders import batch_production as batches, finishing, replenishment
from tests.test_contracts.test_replenishment_list import (
    batch, floor, outside, review_db, shop,  # noqa: F401
    assert_no_planning, body, held_jobs, listed, make_recipe, numbers, plate, queue, restrict, row,
)


@pytest.mark.parametrize('damage', ['identity', 'overallocated', 'cumulative_overallocated'])
def test_finishing_identity_and_total_consumption_are_validated(floor, damage):
    from tests.test_contracts.test_finishing_workflow import bom, component, finish_body
    preview = bom(floor['batch'])
    inspected = component(floor['batch']).json()
    request = finish_body(floor['batch'], inspected, preview)
    answer = floor['client'].post('/products/production/finishing', json=request)
    assert answer.status_code == 200, answer.text
    numbers(floor)
    assert row(floor)['on_the_way']['awaiting_stock'] == 2
    doc, _ = finishing.load(floor['db'], 1)
    finish = doc['finishes'][request['request_id']]
    # The writer keys by request ID, not receipt ID; legitimate entries differ.
    assert request['request_id'] != finish['id']
    if damage == 'identity':
        doc['finishes']['f' * 32] = doc['finishes'].pop(request['request_id'])
    elif damage == 'overallocated':
        finish['allocations'][0]['quantity'] = inspected['accepted'] + 1
    else:
        second_request = 'f' * 32
        second = json.loads(json.dumps(finish))
        second['id'] = uuid.uuid5(uuid.NAMESPACE_URL, f'odin:finish:1:{second_request}').hex
        doc['finishes'][second_request] = second
    floor['db'].execute(update(SystemConfig).where(SystemConfig.key == finishing.key(1))
                        .values(value=batches.packed(doc)))
    floor['db'].commit()
    assert_no_planning(listed(floor))


@pytest.mark.parametrize('damage', ['missing_observation', 'foreign_observation', 'different_recipe'])
def test_outside_binding_validates_retained_reference_and_receipt_recipe(floor, outside, damage):
    from tests.test_contracts.test_outside_print_binding import binding_body
    _, pid, recipe = outside
    answer = floor['client'].post(f'/products/production/outside-prints/{pid}/bind', json=binding_body(recipe))
    assert answer.status_code == 200, answer.text
    request = body(floor['batch'], answer.json()['recipe_revision'])
    request.pop('job_id')
    request['print_job_id'] = pid
    answer = floor['client'].post('/products/production/inspections', json=request)
    assert answer.status_code == 200, answer.text
    numbers(floor, target=20)
    assert row(floor)['on_the_way']['awaiting_stock'] == 4
    db = floor['db']
    detail = json.loads(db.execute(text('SELECT definition FROM production_acceptances')).scalar_one())
    binding = detail['outside_binding']
    if damage == 'different_recipe':
        other = make_recipe(floor['batch'])
        assert other['id'] != recipe['id']
        binding['recipe_id'] = other['id']
        binding['recipe_revision'] = other
    else:
        uid = 'missing-observation'
        if damage == 'foreign_observation':
            from datetime import datetime, timezone
            from core.economic_source_creation import _create_attempt
            db.execute(text("INSERT INTO printers(id,name,org_id) VALUES(400,'Foreign',2)"))
            observed = _create_attempt(db, {'printer_id': 400, 'status': 'running',
                                           'started_at': datetime.now(timezone.utc)})
            uid = db.execute(text('SELECT economic_birth_nonce FROM print_jobs WHERE id=:id'),
                             {'id': observed.id}).scalar_one()
        binding['recipe_revision']['reference_observation_uid'] = uid
    db.execute(update(batches.receipts).values(definition=batches.packed(detail)))
    db.commit()
    assert_no_planning(listed(floor))


@pytest.mark.parametrize('hidden', [False, True])
@pytest.mark.parametrize('stock_problem', ['unmapped', 'unread'])
def test_public_stock_reasons_and_below_point_survive_planning_block(floor, hidden, stock_problem):
    plate(floor)
    numbers(floor)
    numbers(floor, 104)
    if stock_problem == 'unread':
        from tests.test_contracts.test_replenishment_list import remap
        remap(floor, 104, 'V4')
    if hidden:
        restrict(floor)
    else:
        floor['db'].execute(update(batches.recipes).values(definition='{'))
        floor['db'].commit()
    result = listed(floor)
    assert_no_planning(result)
    rows = {r['product_id']: r for r in result['rows']}
    public_reason = 'Map this product' if stock_problem == 'unmapped' else 'no stock count'
    assert public_reason in rows[104]['reason']
    problem = replenishment.HIDDEN_WORK if hidden else 'Recipe scan'
    assert all(problem in r['reason'] for r in rows.values())
    assert rows[101]['below_point'] is True
    assert rows[104]['below_point'] is None


def test_print_next_fix_is_in_the_fixed_changelog_section():
    changelog = (Path(__file__).resolve().parents[2] / 'CHANGELOG.md').read_text()
    unreleased = changelog.split('## [Unreleased]', 1)[1].split('\n## ', 1)[0]
    fixed = unreleased.split('### Fixed', 1)[1].split('\n### ', 1)[0]
    assert 'Fixed Print next:' in fixed


def test_denied_queue_does_not_read_square_or_claim_refresh_interval(floor):
    from tests.test_contracts.test_replenishment_list import join_audience
    recipe = plate(floor)
    numbers(floor)
    restrict(floor)
    reads_before = len(floor['calls'])
    item = {'product_id': 101, 'recipe_id': recipe['id'], 'plates': 1}
    answer = queue(floor, [item])
    assert answer.status_code == 403
    assert answer.json()['detail'] == replenishment.HIDDEN_WORK
    assert len(floor['calls']) == reads_before
    assert floor['state']['reads'] == []
    assert 1 not in replenishment._reads
    assert held_jobs(floor) == []
    join_audience(floor)
    assert queue(floor, [item]).json()['queued']
    assert len(floor['state']['reads']) == 1


@pytest.mark.parametrize('file_owner', [2, None])
def test_queue_refuses_a_file_outside_the_exact_tenant(floor, file_owner):
    recipe = plate(floor)
    numbers(floor)
    floor['db'].execute(text('UPDATE print_files SET org_id=:owner WHERE id=301'), {'owner': file_owner})
    floor['db'].commit()
    answer = queue(floor, [{'product_id': 101, 'recipe_id': recipe['id'], 'plates': 1}])
    assert answer.status_code == 200, answer.text
    assert answer.json()['queued'] == []
    assert 'File ownership scan' in answer.json()['skipped'][0]['reason']
    assert held_jobs(floor) == []
