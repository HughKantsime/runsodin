"""Print-next list and held-job queueing: real HTTP and database, Square reads simulated.

No test opens a network connection: SquareReader._read is replaced by the fixture
transport (as in the other Square tests), and no path writes to Square.
"""
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import Depends, HTTPException
from sqlalchemy import event, text, update

from tests.test_contracts.test_assortment_policy import BASE, ready
from tests.test_contracts.test_batch_acceptance import batch, body, make_recipe  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_square_readonly_workflow import mapped, shop  # noqa: F401
from modules.orders import assortment, batch_production as batches, replenishment, square_store

URL = '/production/replenishment'
COUNTS = '/v2/inventory/counts/batch-retrieve'


@pytest.fixture
def floor(batch, monkeypatch, tmp_path):  # noqa: F811
    """The batch fixture plus the print-next routes, a controllable Square count read and a real plate file."""
    client, user, calls, db, app, upstream, *_ = batch
    from core.registry import registry
    from modules.jobs.queue_creation_provider import QueueCreationService
    from modules.orders.routes import replenishment as routes
    from modules.orders.square_client import SquareReader, refuse_under_itar
    app.include_router(routes.router)
    monkeypatch.setitem(registry._providers, 'QueueCreationProvider', QueueCreationService())
    replenishment.forget_reads()
    state = {'levels': {'V1': '2'}, 'fail': None, 'reads': []}

    def transport(self, method, path, *, params=None, body=None):
        refuse_under_itar()  # as the real transport does, before any network use
        if state['fail'] is not None and path != '/oauth2/token/status':
            raise state['fail']
        if path == '/v2/locations':
            calls.append((method, path, params, body))
            return {'locations': [{'id': name, 'name': name, 'status': 'ACTIVE', 'merchant_id': 'M1'} for name in ('L1', 'L2')]}
        if path == COUNTS:
            calls.append((method, path, params, body))
            state['reads'].append(body)
            return {'counts': [{'catalog_object_id': v, 'location_id': body['location_ids'][0], 'state': 'IN_STOCK',
                                'quantity': state['levels'][v], 'calculated_at': '2026-11-13T08:00:00Z'}
                               for v in body['catalog_object_ids'] if v in state['levels']]}
        return upstream(self, method, path, params=params, body=body)
    monkeypatch.setattr(SquareReader, '_read', transport)
    plate = tmp_path / 'plate.gcode'
    plate.write_bytes(b'G28\nG1 X10\n')
    digest = hashlib.sha256(plate.read_bytes()).hexdigest()
    db.execute(text('UPDATE jobs SET payload_sha256=:sha WHERE id IN (301,302)'), {'sha': digest})
    db.execute(text("INSERT INTO print_files(id,filename,original_filename,project_name,stored_path,org_id,created_by,storage_bytes,"
                    "blob_state,compatibility_facts_json,model_id,print_time_seconds) VALUES(301,'plate.gcode','plate.gcode','Plate',:path,1,3,"
                    "10,'present','{}',301,7200)"), {'path': str(plate)})
    db.commit()
    yield {'client': client, 'user': user, 'calls': calls, 'db': db, 'app': app, 'state': state, 'batch': batch, 'plate': plate}
    replenishment.forget_reads()


def numbers(floor, pid=101, mode='active', point=5, target=10, smallest=1):
    client = floor['client']
    answer = client.put(f'{BASE}/{pid}', json={'revision': client.get(BASE).json()['revision'], 'assortment': mode,
                                             'reorder_point': point, 'target': target, 'minimum_batch': smallest})
    assert answer.status_code == 200, answer.text


def add_product(floor, pid, variation, location='L1', name=None, map_it=True):
    db = floor['db']
    db.execute(text('INSERT INTO products(id,name,org_id) VALUES(:id,:name,1)'), {'id': pid, 'name': name or f'Product {pid}'})
    if map_it:
        remap(floor, pid, variation, location)
    db.commit()


def remap(floor, pid, variation, location='L1'):
    db = floor['db']
    doc, prior = square_store.load(db, 1)
    doc['mappings'][str(pid)] = {'variation_id': variation, 'location_id': location, 'item_name': 'Item', 'variation_name': 'Default'}
    square_store.save(db, 1, doc, prior)
    db.commit()


def plate(floor, yield_count=5, outputs=None):
    """A qualified recipe whose two fixture runs are inspected and confirmed, so nothing is on the way."""
    if outputs:
        recipe = make_recipe(floor['batch'], outputs=outputs)
        floor['db'].execute(text("UPDATE jobs SET status='cancelled' WHERE id IN (301,302)"))
        floor['db'].commit()
    else:
        recipe = ready(floor['batch'], yield_count=yield_count)
    # The stored test plate is plain G-code: plate selection inside a package is a Bambu-only check.
    floor['db'].execute(text("UPDATE printers SET api_type='moonraker' WHERE id=301"))
    floor['db'].commit()
    return recipe


def listed(floor, **params):
    answer = floor['client'].get(URL, params=params)
    assert answer.status_code == 200, answer.text
    return answer.json()


def row(floor, pid=101, **params):
    return next(r for r in listed(floor, **params)['rows'] if r['product_id'] == pid)


def later(monkeypatch, seconds):
    """Advance the clock the two-minute limit uses."""
    import time
    start = time.monotonic()
    monkeypatch.setattr(replenishment.time, 'monotonic', lambda: start + seconds)


def queue(floor, items, ready=None):
    payload = {'items': items}
    if ready is not None:
        payload['ready'] = ready
    return floor['client'].post(URL + '/queue', json=payload)


def held_jobs(floor):
    return floor['db'].execute(text("SELECT id,status,hold,print_file_id,plate_index,printer_id,target_type,target_filter,model_id,"
                                    "charged_to_org_id,quantity_on_bed,item_name FROM jobs WHERE id>302 ORDER BY id")).mappings().all()


# ---- arithmetic -------------------------------------------------------------------------------

@pytest.mark.parametrize('shortfall,per_plate,smallest,plates', [
    (0, 5, 1, 0), (1, 5, 1, 1), (5, 5, 1, 1), (6, 5, 1, 2), (11, 5, 1, 3), (3, 4, 8, 2), (9, 4, 8, 3), (1, 1, 1, 1)])
def test_plates_needed_rounds_up_and_respects_the_smallest_batch(shortfall, per_plate, smallest, plates):
    assert replenishment.plates_for(shortfall, per_plate, smallest) == plates


def test_shortfall_is_target_minus_on_hand_and_plates_round_up(floor):
    recipe = plate(floor)
    numbers(floor, point=5, target=14)
    floor['state']['levels']['V1'] = '2'
    entry = row(floor)
    assert entry['status'] == 'below' and entry['below_point'] is True
    assert entry['on_hand']['quantity'] == 2 and entry['on_hand']['source'] == 'square'
    assert (entry['reorder_point'], entry['target'], entry['minimum_batch']) == (5, 14, 1)
    assert entry['shortfall'] == 12 and entry['plates_needed'] == 3  # ceil(12 / 5)
    assert entry['recipe'] == {'id': recipe['id'], 'name': 'Qualified part', 'pieces_per_plate': 5, 'printer_id': 301,
                               'materials': 'PLA black, 0.4 nozzle', 'hours_per_plate': 2.0, 'hours_per_piece': 0.4}
    assert entry['on_the_way'] == {'pieces': 0, 'job_ids': [], 'awaiting_stock': 0}


def test_pieces_already_on_the_way_reduce_the_shortfall(floor):
    plate(floor)
    numbers(floor, point=5, target=14)
    # One matching run is back in the queue, one is printing: 10 pieces are coming.
    floor['db'].execute(text("INSERT INTO jobs(id,item_name,model_id,status,charged_to_org_id,quantity) VALUES(401,'run',301,'pending',1,1),(402,'run',301,'printing',1,1)"))
    floor['db'].commit()
    entry = row(floor)
    assert entry['on_the_way'] == {'pieces': 10, 'job_ids': [401, 402], 'awaiting_stock': 0}
    assert entry['shortfall'] == 2 and entry['plates_needed'] == 1
    floor['db'].execute(text("INSERT INTO jobs(id,item_name,model_id,status,charged_to_org_id,quantity) VALUES(403,'run',301,'scheduled',1,1)"))
    floor['db'].commit()
    entry = row(floor)
    assert entry['status'] == 'below' and entry['shortfall'] == 0 and entry['plates_needed'] == 0 and entry['rank'] is None
    assert 'already on the way' in entry['reason']
    # Failed and cancelled runs are not production; a job of another organization is not ours.
    floor['db'].execute(text("UPDATE jobs SET status='failed' WHERE id=401"))
    floor['db'].execute(text("UPDATE jobs SET status='cancelled' WHERE id=402"))
    floor['db'].execute(text("UPDATE jobs SET charged_to_org_id=2 WHERE id=403"))
    floor['db'].commit()
    assert row(floor)['on_the_way']['pieces'] == 0


def test_uninspected_output_and_pieces_waiting_for_square_count_as_on_the_way(floor):
    recipe = make_recipe(floor['batch'])
    numbers(floor, point=5, target=20)
    # Both completed fixture runs are uninspected: 2 x 5 pieces.
    assert row(floor)['on_the_way'] == {'pieces': 10, 'job_ids': [301, 302], 'awaiting_stock': 0}
    request = body(floor['batch'], recipe)
    assert floor['client'].post('/products/production/inspections', json=request).status_code == 200
    # 302 is inspected (4 accepted, not yet added in Square); 301 still awaits inspection.
    entry = row(floor)
    assert entry['on_the_way'] == {'pieces': 9, 'job_ids': [301], 'awaiting_stock': 4}
    assert entry['shortfall'] == 20 - 2 - 9


def test_smallest_batch_excludes_a_small_plate_and_names_what_is_missing(floor):
    plate(floor, yield_count=3)
    numbers(floor, point=5, target=10, smallest=4)
    entry = row(floor)
    assert entry['status'] == 'blocked' and entry['below_point'] is True and entry['recipe'] is None and entry['plates_needed'] == 0
    assert 'no qualified recipe' in entry['reason']
    numbers(floor, point=5, target=10, smallest=3)
    entry = row(floor)
    assert entry['status'] == 'below' and entry['recipe']['pieces_per_plate'] == 3 and entry['plates_needed'] == 3  # ceil(8 / 3)


def test_legacy_policy_without_numbers_prints_one_batch(floor):
    plate(floor)
    doc, prior = assortment.load(floor['db'], 1)
    doc['products']['101'] = {'assortment': 'active', 'kind': 'dragon_jumbo'}
    assortment.save(floor['db'], 1, doc, prior)
    floor['db'].commit()
    floor['state']['levels']['V1'] = '14'
    entry = row(floor)
    assert (entry['reorder_point'], entry['target']) == (15, 15) and entry['shortfall'] == 1 and entry['plates_needed'] == 1
    replenishment.forget_reads()
    floor['state']['levels']['V1'] = '15'
    assert row(floor)['status'] == 'ok'


@pytest.mark.parametrize('level,reason', [('-1', 'negative or fractional'), ('1.5', 'negative or fractional'), (None, 'no stock count')])
def test_unknown_negative_or_fractional_counts_are_blocked_not_guessed(floor, level, reason):
    plate(floor)
    numbers(floor)
    if level is None:
        floor['state']['levels'].clear()
    else:
        floor['state']['levels']['V1'] = level
    entry = row(floor)
    assert entry['status'] == 'blocked' and reason in entry['reason'] and entry['plates_needed'] == 0 and entry['shortfall'] is None


# ---- ranking ----------------------------------------------------------------------------------

def test_ranking_orders_first_then_zero_stock_then_share_of_target(floor):
    db = floor['db']
    # One plate makes every product, so each has a qualified recipe; nothing is on the way.
    for pid, variation in ((105, 'V5'), (106, 'V6'), (107, 'V7'), (108, 'V8'), (109, 'V9')):
        add_product(floor, pid, variation)
    remap(floor, 104, 'V4')
    products = (101, 104, 105, 106, 107, 108, 109)
    plate(floor, outputs=[{'product_id': pid, 'kind': 'main', 'expected': 5} for pid in products])
    for pid in products:
        numbers(floor, pid, point=10, target=20)
    floor['state']['levels'] = {'V1': '8',   # 60% of target missing
                                'V4': '0',   # out of stock
                                'V5': '12',  # above its point, but an old order needs 30
                                'V6': '15',  # above its point: nothing to do
                                'V7': '4',   # 80% missing
                                'V8': '9',   # below, with a newer order stock cannot cover
                                'V9': '3'}   # below, order fully covered by stock: ranks by share only
    old, new = datetime(2026, 10, 1), datetime(2026, 11, 1)
    db.execute(text("INSERT INTO orders(id,status,org_id,order_date) VALUES(1,'pending',1,:old),(2,'in_progress',1,:new),(3,'pending',1,:new),"
                    "(4,'shipped',1,:old),(5,'cancelled',1,:old),(6,'pending',2,:old)"), {'old': old, 'new': new})
    db.execute(text("INSERT INTO order_items(order_id,product_id,quantity,fulfilled_quantity) VALUES(1,105,30,0),(2,108,12,1),(3,109,2,0),"
                    "(4,104,99,0),(5,104,99,0),(6,106,99,0)"))
    db.commit()
    result = listed(floor)
    ranked = [(r['product_id'], r['rank']) for r in result['rows'] if r['rank']]
    assert ranked == [(105, 1), (108, 2), (104, 3), (109, 4), (107, 5), (101, 6)]
    by_id = {r['product_id']: r for r in result['rows']}
    assert by_id[105]['rank_reason'] == 'Open orders need 30 and stock is 12.' and by_id[105]['shortfall'] == 18 and by_id[105]['below_point'] is False
    assert by_id[108]['open_orders']['pieces'] == 11 and by_id[108]['shortfall'] == 11  # max(target 20, orders 11) - 9
    assert by_id[104]['rank_reason'] == 'Out of stock.' and by_id[104]['open_orders'] is None  # shipped and cancelled orders are not open
    assert by_id[107]['rank_reason'] == '80% of its target is missing.' and by_id[109]['rank_reason'] == '85% of its target is missing.'
    assert by_id[106]['status'] == 'ok' and by_id[106]['rank'] is None and by_id[106]['open_orders'] is None  # another organization's order
    assert [r['product_id'] for r in result['rows']][-1] == 106 and result['print_next_count'] == 6


def test_ties_break_on_cheapest_hours_per_piece(floor):
    plate(floor)  # 101: 5 pieces in 2 h
    add_product(floor, 105, 'V5', name='Aardvark')
    db = floor['db']
    # A second file and plate for 105: 1 piece in 2 h, so dearer per piece.
    db.execute(text("INSERT INTO print_files(id,filename,original_filename,project_name,stored_path,org_id,created_by,storage_bytes,blob_state,"
                    "compatibility_facts_json,model_id,print_time_seconds) VALUES(302,'b.gcode','b.gcode','B','/nowhere/b.gcode',1,3,10,'present','{}',301,7200)"))
    definition = json.loads(db.execute(text('SELECT definition FROM production_recipes')).scalar())
    definition['payload']['print_file_id'] = 302
    definition['outputs'] = [{'product_id': 105, 'kind': 'main', 'expected': 1, 'product_name': 'Aardvark', 'bom_model_id': None}]
    db.execute(batches.recipes.insert().values(id='f' * 32, owner=1, definition=batches.packed(definition)))
    db.commit()
    numbers(floor, 101, point=10, target=10)
    numbers(floor, 105, point=10, target=10)
    floor['state']['levels'] = {'V1': '5', 'V5': '5'}
    ranked = [r['product_id'] for r in listed(floor)['rows'] if r['rank']]
    assert ranked == [101, 105]  # equal share missing; 0.4 h per piece beats 2 h although 'Aardvark' sorts first


# ---- stock reads: one batch per location, two-minute cache, Square down ----------------------------

def test_one_batch_read_per_location_regardless_of_product_count(floor):
    plate(floor)
    numbers(floor)
    for index in range(40):
        pid = 500 + index
        add_product(floor, pid, f'A{index}', 'L1' if index % 2 else 'L2')
        floor['state']['levels'][f'A{index}'] = str(index)
    doc, prior = assortment.load(floor['db'], 1)
    for index in range(40):
        doc['products'][str(500 + index)] = {'assortment': 'active', 'reorder_point': 5, 'target': 9, 'minimum_batch': 1}
    assortment.save(floor['db'], 1, doc, prior)
    floor['db'].commit()
    before = len(floor['calls'])
    result = listed(floor)
    assert len(result['rows']) == 41 and all(r['on_hand'] for r in result['rows'])
    made = floor['calls'][before:]
    reads = [c for c in made if c[1] == COUNTS]
    assert len(reads) == 2 and sorted(c[3]['location_ids'][0] for c in reads) == ['L1', 'L2']
    assert sorted(len(c[3]['catalog_object_ids']) for c in reads) == [20, 21]
    # Besides the two count reads: one account check and one location check. Nothing per product, and only reads.
    assert sorted(c[1] for c in made) == sorted([COUNTS, COUNTS, '/v2/locations', '/v2/merchants/me'])
    assert all(c[0] == 'GET' or c[1] == COUNTS for c in made)


def test_database_queries_do_not_grow_with_the_product_count(floor):
    plate(floor)
    numbers(floor)

    def cost():
        seen = []
        def note(conn, cursor, statement, *_):
            seen.append(statement)
        engine = floor['db'].get_bind()
        event.listen(engine, 'before_cursor_execute', note)
        try:
            listed(floor, cached='true')
        finally:
            event.remove(engine, 'before_cursor_execute', note)
        return len(seen)
    few = cost()
    for index in range(30):
        add_product(floor, 600 + index, f'B{index}')
    doc, prior = assortment.load(floor['db'], 1)
    for index in range(30):
        doc['products'][str(600 + index)] = {'assortment': 'active', 'reorder_point': 5, 'target': 9, 'minimum_batch': 1}
    assortment.save(floor['db'], 1, doc, prior)
    floor['db'].commit()
    assert len(listed(floor, cached='true')['rows']) == 31
    assert cost() == few


def test_counts_are_read_at_most_once_per_two_minutes(floor, monkeypatch):
    plate(floor)
    numbers(floor)
    first = listed(floor)
    assert len(floor['state']['reads']) == 1 and first['stock']['source'] == 'square' and first['stock']['cache_seconds'] == 120
    assert 0 < first['stock']['refresh_in_seconds'] <= 120
    floor['state']['levels']['V1'] = '9'
    again = listed(floor)  # the Refresh button inside the interval
    assert len(floor['state']['reads']) == 1 and again['rows'][0]['on_hand']['quantity'] == 2
    assert again['stock']['read_at'] == first['stock']['read_at']
    later(monkeypatch, 119)
    listed(floor)
    assert len(floor['state']['reads']) == 1
    later(monkeypatch, 121)
    fresh = listed(floor)
    assert len(floor['state']['reads']) == 2 and fresh['rows'][0]['on_hand']['quantity'] == 9 and fresh['rows'][0]['status'] == 'ok'


def test_the_limit_is_per_organization_and_cached_never_reads(floor, monkeypatch):
    plate(floor)
    numbers(floor)
    assert listed(floor, cached='true')['stock']['source'] == 'none' and floor['state']['reads'] == []
    assert row(floor, cached='true')['status'] == 'blocked'
    listed(floor)
    assert len(floor['state']['reads']) == 1
    # Another organization's own connection is read on its own clock.
    floor['user'].update(id=5, group_id=2)
    from tests.test_contracts.test_square_readonly_workflow import connect
    status, _ = connect(floor['client'])
    assert floor['client'].put('/products/102/square', json={'revision': status['revision'], 'location_id': 'L1', 'variation_id': 'V1'}).status_code == 200
    floor['client'].put(f'{BASE}/102', json={'revision': None, 'assortment': 'active', 'reorder_point': 5, 'target': 5, 'minimum_batch': 1})
    other = listed(floor)
    assert [r['product_id'] for r in other['rows']] == [102] and len(floor['state']['reads']) == 2


def test_square_down_shows_the_last_snapshot_with_its_age_and_blocks_products_without_one(floor, monkeypatch):
    plate(floor)
    numbers(floor)
    add_product(floor, 105, 'V5')
    numbers(floor, 105)
    seen = datetime.now(timezone.utc) - timedelta(hours=3)
    floor['db'].execute(text("INSERT INTO stock_snapshots(owner_scope,owner_key,variation,location,quantity,calculated_at,read_at) "
                             "VALUES('org',1,'V1','L1','3',:at,:at)"), {'at': seen})
    floor['db'].commit()
    floor['state']['fail'] = HTTPException(502, 'Square is unavailable or returned invalid data. No stock was inferred.')
    result = listed(floor)
    assert result['stock']['source'] == 'snapshot' and 'Square is unavailable' in result['stock']['error']
    by_id = {r['product_id']: r for r in result['rows']}
    assert by_id[101]['on_hand']['quantity'] == 3 and by_id[101]['on_hand']['source'] == 'snapshot'
    assert 3 * 3600 - 5 <= by_id[101]['on_hand']['age_seconds'] <= 3 * 3600 + 60
    assert by_id[101]['status'] == 'below' and by_id[101]['plates_needed'] == 2  # still usable advice, clearly dated
    assert by_id[105]['status'] == 'blocked' and by_id[105]['on_hand'] is None and 'No stock count' in by_id[105]['reason']
    # A failed attempt also waits out the interval instead of hammering Square.
    attempts = []
    monkeypatch.setattr(replenishment, 'read_counts', lambda *a: attempts.append(1) or {})
    listed(floor)
    assert attempts == []


def test_itar_mode_shows_the_snapshot_not_an_error_and_opens_no_connection(floor, monkeypatch):
    import socket
    from modules.orders.square_client import ITAR_REFUSAL
    plate(floor)
    numbers(floor)
    listed(floor)  # one ordinary read records the snapshot
    replenishment.forget_reads()
    reads = len(floor['state']['reads'])

    def refuse(*args, **kwargs):
        raise AssertionError('a socket was opened')
    monkeypatch.setattr(socket, 'create_connection', refuse)
    monkeypatch.setattr(socket, 'getaddrinfo', refuse)
    monkeypatch.setenv('ODIN_ITAR_MODE', '1')
    result = floor['client'].get(URL)
    assert result.status_code == 200, result.text
    data = result.json()
    assert data['stock']['error'] == ITAR_REFUSAL and data['stock']['source'] == 'snapshot' and data['stock']['read_at'] is None
    entry = data['rows'][0]
    assert entry['on_hand']['quantity'] == 2 and entry['on_hand']['source'] == 'snapshot' and entry['status'] == 'below'
    assert entry['on_hand']['age_seconds'] >= 0 and len(floor['state']['reads']) == reads


def test_a_reconnect_during_the_read_refuses_the_stale_list(floor, monkeypatch):
    plate(floor)
    numbers(floor)
    from sqlalchemy.orm import Session

    def race(doc, pairs):
        with Session(floor['db'].get_bind()) as db:
            current, prior = square_store.load(db, 1)
            square_store.save(db, 1, current, prior)
            db.commit()
        return {}
    monkeypatch.setattr(replenishment, 'read_counts', race)
    assert floor['client'].get(URL).status_code == 409


def test_the_list_never_writes_to_square_or_calls_an_unlisted_operation(floor):
    plate(floor)
    numbers(floor)
    before = len(floor['calls'])
    listed(floor)
    assert queue(floor, [{'product_id': 101, 'recipe_id': row(floor)['recipe']['id'], 'plates': 1}]).status_code == 200
    allowed = {('GET', '/v2/merchants/me'), ('GET', '/v2/locations'), ('POST', COUNTS)}
    assert {(c[0], c[1]) for c in floor['calls'][before:]} <= allowed
    assert floor['db'].execute(text('SELECT COUNT(*) FROM production_stock_additions WHERE state NOT IN (\'confirmed\')')).scalar() == 0


# ---- which products are listed ----------------------------------------------------------------------

@pytest.mark.parametrize('mode', ['inactive', 'trial'])
def test_inactive_and_trial_products_are_never_listed_as_below(floor, mode):
    plate(floor)
    numbers(floor, mode=mode)
    floor['state']['levels']['V1'] = '0'
    result = listed(floor)
    assert result['rows'] == [] and result['print_next_count'] == 0 and floor['state']['reads'] == []
    assert queue(floor, [{'product_id': 101, 'recipe_id': 'a' * 32, 'plates': 1}]).json() == {
        'queued': [], 'skipped': [{'product_id': 101, 'reason': 'This product is not in the active assortment.'}]}


def test_unmapped_and_unconfigured_products(floor):
    plate(floor)
    numbers(floor)
    numbers(floor, 104)  # owned, active, not mapped to Square
    result = listed(floor)
    by_id = {r['product_id']: r for r in result['rows']}
    assert set(by_id) == {101, 104}  # 102 and 103 belong elsewhere; products without a policy are not listed
    assert by_id[104]['status'] == 'blocked' and by_id[104]['reason'] == 'Map this product to a Square item to see its stock.'
    assert by_id[104]['on_hand'] is None and by_id[104]['plates_needed'] == 0
    floor['db'].execute(text('DELETE FROM system_config WHERE key=:key'), {'key': square_store.key(1)})
    floor['db'].commit()
    result = listed(floor)
    assert result['stock']['connected'] is False
    assert all(r['status'] == 'blocked' and r['reason'].startswith('Connect Square') for r in result['rows'])


def test_organization_scoping(floor):
    plate(floor)
    numbers(floor)
    assert [r['product_id'] for r in listed(floor)['rows']] == [101]
    floor['user'].update(id=5, group_id=2)
    assert listed(floor)['rows'] == []  # no policy, connection, recipe or job of organization 1 is visible
    recipe_id = floor['db'].execute(text('SELECT id FROM production_recipes')).scalar()
    answer = queue(floor, [{'product_id': 101, 'recipe_id': recipe_id, 'plates': 1}])
    assert answer.status_code in (404, 409) and held_jobs(floor) == []
    floor['user'].update(id=9, group_id=None, role='operator')
    assert floor['client'].get(URL).status_code == 403


@pytest.mark.parametrize('role,auth,scopes,read,write', [
    ('admin', 'jwt', None, 200, 200), ('operator', 'jwt', None, 200, 200), ('viewer', 'jwt', None, 403, 403),
    ('operator', 'user_api_token', ['read'], 200, 403), ('operator', 'user_api_token', ['write:jobs'], 403, 200),
    ('operator', 'user_api_token', ['read:printers'], 403, 403), ('viewer', 'user_api_token', ['read'], 403, 403)])
def test_rbac_for_the_list_and_the_queue(floor, role, auth, scopes, read, write):
    recipe = plate(floor)
    numbers(floor)
    floor['user'].update(role=role, _auth_kind=auth)
    if scopes is not None:
        floor['user']['_token_scopes'] = scopes
    assert floor['client'].get(URL, params={'cached': 'true'}).status_code == read
    assert queue(floor, [{'product_id': 101, 'recipe_id': recipe['id'], 'plates': 1}]).status_code == write


def test_permission_matrix_can_withdraw_the_list_from_operators(floor):
    recipe = plate(floor)
    numbers(floor)
    from core.models import SystemConfig
    floor['user']['role'] = 'operator'
    assert floor['client'].get(URL, params={'cached': 'true'}).status_code == 200
    floor['db'].add(SystemConfig(key='rbac_permissions', value={'action_access': {'products.assortment_preview': ['admin']}}))
    floor['db'].commit()
    assert floor['client'].get(URL, params={'cached': 'true'}).status_code == 403
    assert queue(floor, [{'product_id': 101, 'recipe_id': recipe['id'], 'plates': 1}]).status_code == 403
    # Queueing also needs the right to create jobs.
    floor['db'].execute(text("DELETE FROM system_config WHERE key='rbac_permissions'"))
    floor['db'].add(SystemConfig(key='rbac_permissions', value={'action_access': {'jobs.create': ['admin']}}))
    floor['db'].commit()
    assert floor['client'].get(URL, params={'cached': 'true'}).status_code == 200
    assert queue(floor, [{'product_id': 101, 'recipe_id': recipe['id'], 'plates': 1}]).status_code == 403
    assert held_jobs(floor) == []


def test_licence_gate_needs_pro_for_the_list_and_the_queue(floor, tier_license):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.orders.routes import router
    gated = FastAPI()
    gated.include_router(router)
    for dependency in (get_db, get_current_user):
        gated.dependency_overrides[dependency] = floor['app'].dependency_overrides[dependency]
    client = TestClient(gated)
    recipe = plate(floor)
    numbers(floor)
    item = {'items': [{'product_id': 101, 'recipe_id': recipe['id'], 'plates': 1}]}
    tier_license(None)
    assert client.get(URL).status_code == 403 and client.post(URL + '/queue', json=item).status_code == 403
    assert floor['state']['reads'] == [] and held_jobs(floor) == []
    tier_license('pro')
    assert client.get(URL).status_code == 200 and client.post(URL + '/queue', json=item).status_code == 200


def test_controlled_plate_follows_existing_visibility(floor, monkeypatch):
    recipe = plate(floor)
    numbers(floor)
    from core import controlled_parts
    monkeypatch.setattr(controlled_parts, 'any_controlled_data', lambda db: True)
    monkeypatch.setattr(controlled_parts, 'allowed', lambda db, kind, identifier, actor: not (kind == 'model' and identifier == 301))
    entry = row(floor)
    assert entry['status'] == 'blocked' and entry['recipe'] is None and 'controlled work you cannot see' in entry['reason']
    assert 'Qualified part' not in json.dumps(entry)
    answer = queue(floor, [{'product_id': 101, 'recipe_id': recipe['id'], 'plates': 1}])
    assert answer.json()['queued'] == [] and held_jobs(floor) == []
    monkeypatch.setattr(controlled_parts, 'allowed', lambda *a: True)
    assert row(floor)['recipe']['id'] == recipe['id']


# ---- queue ----------------------------------------------------------------------------------------

def test_queue_creates_held_jobs_through_the_shared_queue_path_and_a_second_click_adds_none(floor, monkeypatch):
    recipe = plate(floor)
    numbers(floor, point=5, target=14)
    from modules.jobs.queue_creation_provider import QueueCreationService
    staged = []
    original = QueueCreationService.stage_job
    monkeypatch.setattr(QueueCreationService, 'stage_job', lambda self, db, job, actor: staged.append(job) or original(self, db, job, actor))
    entry = row(floor)
    assert entry['plates_needed'] == 3
    item = {'product_id': 101, 'recipe_id': recipe['id'], 'plates': 3}
    answer = queue(floor, [item])
    assert answer.status_code == 200, answer.text
    result = answer.json()
    jobs = held_jobs(floor)
    assert len(jobs) == 3 == len(staged) and result['skipped'] == []
    assert result['queued'] == [{'product_id': 101, 'recipe_id': recipe['id'], 'plates': 3, 'job_ids': [j['id'] for j in jobs], 'held': True}]
    for job in jobs:
        assert job['status'] == 'pending' and bool(job['hold']) is True
        # The recipe's exact file, plate and printer, owned by this organization.
        assert (job['print_file_id'], job['plate_index'], job['printer_id'], job['model_id']) == (301, 0, 301, 301)
        assert (job['target_type'], job['target_filter'], job['charged_to_org_id'], job['quantity_on_bed']) == ('specific', '301', 1, 5)
        assert job['item_name'] == 'Qualified part'
    payload = floor['db'].execute(text('SELECT payload_sha256,economic_uid FROM jobs WHERE id=:id'), {'id': jobs[0]['id']}).one()
    assert payload[0] == hashlib.sha256(floor['plate'].read_bytes()).hexdigest() and payload[1]  # queue identity attested like any new job
    # The list and the one-product check now see them.
    entry = row(floor)
    assert entry['on_the_way']['pieces'] == 15 and entry['on_the_way']['job_ids'] == [j['id'] for j in jobs]
    assert entry['shortfall'] == 0 and entry['plates_needed'] == 0
    assert floor['client'].post(f'{BASE}/101/preview').json()['status'] == 'pending_review'
    # Second click, same request: nothing new.
    again = queue(floor, [item])
    assert again.status_code == 200 and again.json()['queued'] == [] and 'already on the way' in again.json()['skipped'][0]['reason']
    assert len(held_jobs(floor)) == 3
    audit = floor['db'].execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='production.replenishment_queued'")).scalar()
    assert audit == 1


def test_queue_never_exceeds_what_is_needed_and_ready_releases_the_hold(floor):
    recipe = plate(floor)
    numbers(floor, point=5, target=10)  # 8 short: 2 plates
    answer = queue(floor, [{'product_id': 101, 'recipe_id': recipe['id'], 'plates': 9}], ready=True)
    assert answer.json()['queued'][0]['plates'] == 2 and answer.json()['queued'][0]['held'] is False
    jobs = held_jobs(floor)
    assert len(jobs) == 2 and all(not job['hold'] and job['status'] == 'pending' for job in jobs)


def test_queue_all_counts_a_mixed_plate_once(floor):
    remap(floor, 104, 'V4')
    recipe = plate(floor, outputs=[{'product_id': 101, 'kind': 'main', 'expected': 5}, {'product_id': 104, 'kind': 'main', 'expected': 5}])
    numbers(floor, 101, point=5, target=10)
    numbers(floor, 104, point=5, target=10)
    floor['state']['levels'] = {'V1': '2', 'V4': '2'}
    rows = listed(floor)['rows']
    assert [r['plates_needed'] for r in rows] == [2, 2]
    answer = queue(floor, [{'product_id': r['product_id'], 'recipe_id': recipe['id'], 'plates': r['plates_needed']} for r in rows])
    result = answer.json()
    assert [q['product_id'] for q in result['queued']] == [101] and [s['product_id'] for s in result['skipped']] == [104]
    assert len(held_jobs(floor)) == 2  # the two plates for 101 also make the 10 pieces 104 needed


@pytest.mark.parametrize('damage,reason', [
    ('edit', 'changed since it was qualified'), ('remove', 'no longer available'), ('printer', 'no longer in this organization')])
def test_queue_refuses_a_plate_whose_file_or_printer_no_longer_matches_the_recipe(floor, damage, reason):
    recipe = plate(floor)
    numbers(floor)
    if damage == 'edit':
        floor['plate'].write_bytes(b'G28\nG1 X99\n')
    elif damage == 'remove':
        floor['plate'].unlink()
    else:
        floor['db'].execute(text('UPDATE printers SET org_id=2 WHERE id=301'))
        floor['db'].commit()
    answer = queue(floor, [{'product_id': 101, 'recipe_id': recipe['id'], 'plates': 1}])
    assert answer.status_code == 200 and answer.json()['queued'] == [] and reason in answer.json()['skipped'][0]['reason']
    assert held_jobs(floor) == []


@pytest.mark.parametrize('items', [
    [], [{'product_id': 101, 'recipe_id': 'nope', 'plates': 1}], [{'product_id': 101, 'recipe_id': 'a' * 32, 'plates': 0}],
    [{'product_id': 101, 'recipe_id': 'a' * 32, 'plates': 51}], [{'product_id': 101, 'recipe_id': 'a' * 32, 'plates': 1.5}],
    [{'product_id': 101, 'recipe_id': 'a' * 32, 'plates': 1, 'printer_id': 2}]])
def test_invalid_queue_requests_are_refused(floor, items):
    plate(floor)
    numbers(floor)
    assert queue(floor, items).status_code == 422 and held_jobs(floor) == []


def test_a_changed_suggestion_or_duplicate_product_is_not_queued(floor):
    recipe = plate(floor)
    numbers(floor)
    answer = queue(floor, [{'product_id': 101, 'recipe_id': 'b' * 32, 'plates': 1}])
    assert answer.json()['queued'] == [] and 'Reload' in answer.json()['skipped'][0]['reason']
    item = {'product_id': 101, 'recipe_id': recipe['id'], 'plates': 1}
    assert queue(floor, [item, item]).status_code == 422 and held_jobs(floor) == []


# ---- velocity and Home Assistant -------------------------------------------------------------------------

def test_sales_velocity_from_orders_needs_fourteen_days_of_history(floor):
    plate(floor)
    numbers(floor)
    db = floor['db']
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.execute(text("INSERT INTO orders(id,status,org_id,order_date) VALUES(1,'shipped',1,:recent),(2,'cancelled',1,:recent)"), {'recent': now - timedelta(days=3)})
    db.execute(text('INSERT INTO order_items(order_id,product_id,quantity,fulfilled_quantity) VALUES(1,101,8,8),(2,101,50,0)'))
    db.commit()
    assert row(floor)['velocity'] is None  # three days of orders: too little to say
    db.execute(text("INSERT INTO orders(id,status,org_id,order_date) VALUES(3,'shipped',1,:old),(4,'shipped',1,:ancient)"),
               {'old': now - timedelta(days=20), 'ancient': now - timedelta(days=60)})
    db.execute(text('INSERT INTO order_items(order_id,product_id,quantity,fulfilled_quantity) VALUES(3,101,4,4),(4,101,100,100)'))
    db.commit()
    assert row(floor)['velocity'] == {'units_per_week': 3.0, 'source': 'ODIN orders', 'weeks': 4}  # (8 + 4) / 4; cancelled and older excluded


def test_print_next_count_service_is_pro_only_and_never_contacts_square(floor, tier_license):
    from modules.orders.services import print_next_count
    plate(floor)
    numbers(floor)
    tier_license(None)
    assert print_next_count(floor['db'], floor['user']) is None
    tier_license('pro')
    assert print_next_count(floor['db'], floor['user']) == 0 and floor['state']['reads'] == []  # no count recorded yet: nothing is claimed
    listed(floor)
    reads = len(floor['state']['reads'])
    assert print_next_count(floor['db'], floor['user']) == 1 and len(floor['state']['reads']) == reads
    assert print_next_count(floor['db'], {'id': 9, 'role': 'viewer', 'group_id': None}) is None
