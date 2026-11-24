"""Slice 3 write permissions through real HTTP routes, with disposable stock and history."""
import pytest
from sqlalchemy import text

from tests.test_contracts.test_archive_reprint_behavior import reprint_client
from tests.test_contracts.test_education_classes_migration import pg_engine
from tests.test_contracts.test_unscoped_jobs_and_models import (
    SCOPES, cert, check, detail, principal, review_db, signed_license,
)


@pytest.fixture
def routes(cert, monkeypatch):
    from core.registry import registry
    from modules.archives.routes import archives_crud, projects, tags
    from modules.inventory.routes import catalogue, conflicts, filament_slots, rfid_onboarding, spool_ops, spools
    from modules.orders.production import OrderFulfillmentService
    from modules.orders.routes import orders_crud, products
    from modules.system import routes_maintenance
    from modules.vision.routes import detections

    client, as_user, _ = cert
    monkeypatch.setitem(registry._providers, "OrderFulfillmentProvider", OrderFulfillmentService())
    for module in (spool_ops, spools, rfid_onboarding, conflicts, filament_slots, orders_crud, products, archives_crud,
                   projects, tags, routes_maintenance, detections):
        client.app.include_router(module.router)
    client.app.include_router(catalogue.slot_router)
    return client, as_user


@pytest.fixture(params=SCOPES + [('operator1', 1, True, 1, 200)])
def access(routes, review_db, request):
    client, as_user = routes
    username, group, organizations, owner, status = request.param
    headers = principal(review_db, as_user, username, group, organizations)

    def call(method, path, body=None, *, expect=status, noun=None, admin_only=False):
        response = client.request(method, path, json=body, headers=headers)
        role_denied = admin_only and username != 'admin'
        check(response, 403 if role_denied else expect, None if role_denied else noun)
        return response

    return call, owner, status, username == 'admin' and group is None


def test_stock(access, review_db):
    from core.audit import log_audit
    from modules.inventory.models import FilamentLibrary, Spool
    from modules.printers.models import FilamentSlot, Printer
    call, owner, status, _ = access
    review_db.add_all([
        FilamentLibrary(id=721, brand='Bambu Lab', name='PLA Basic', material='PLA'),
        FilamentLibrary(id=722, brand='Bambu Lab', name='Blue', material='PLA',
                        catalogue_code='blue', color_hex='ABCDEF'),
    ])
    review_db.flush()
    review_db.add_all([Spool(id=i, filament_id=721, org_id=owner,
                             qr_code=f'roll-{i}', tray_uuid='a' * 32 if i == 724 else None) for i in (723, 724, 725)])
    review_db.flush()
    review_db.add(Printer(id=720, name="Bench printer", org_id=None, slot_count=1))
    review_db.flush()
    review_db.add(FilamentSlot(printer_id=720, slot_number=1, assigned_spool_id=723,
                               filament_type='PLA', color_hex='ABCDEF'))
    review_db.execute(text("INSERT INTO system_config(key,value) VALUES('inventory.sealed.725','true')"))
    log_audit(review_db, 'inventory.rfid_created', 'spool', 724,
              {'possible_duplicate': True, 'possible_duplicate_spool_ids': [725]})
    review_db.commit()

    readable = 404 if status == 404 else 200
    call('GET', '/spools/723', expect=readable)
    call('POST', '/spools/723/use', {'grams': 10}, expect=readable)
    call('POST', '/spools/723/dry?duration_hours=2', expect=readable)
    call('POST', '/spools/723/weigh', {'gross_weight_g': 1050}, expect=readable)
    call('PATCH', '/spools/723', {'color_name': 'Blue'}, noun='spool')
    call('PATCH', '/spools/723', {'spoolman_spool_id': 72}, noun='spool')
    call('PATCH', '/spools/723', {'spoolman_spool_id': None}, noun='spool')
    call('POST', '/printers/720/slots/1/catalogue', {'spool_id': 723, 'filament_id': 722}, noun='spool')
    call('POST', '/spools/rfid-onboarding/724/merge', {'sealed_spool_id': 725}, noun='spool')
    call('POST', '/spools/723/load', {'printer_id': 720, 'slot_number': 1}, expect=readable)
    call('POST', '/spools/723/unload', expect=readable)
    scan = call('POST', '/spools/scan-assign', {'printer_id': 720, 'slot': 1, 'qr_code': 'roll-723'}, expect=200)
    assert scan.json()['success'] == (readable == 200)
    call('POST', '/printers/720/slots/1/assign?spool_id=723', expect=readable)
    call('POST', '/inventory/conflicts/resolve', {'spool_id': 723}, expect=readable, admin_only=True)
    call('DELETE', '/spools/723', noun='spool')
    if status != 200:
        stored = review_db.execute(text('SELECT filament_id,color_name,status,spoolman_spool_id FROM spools '
                                        'WHERE id=723')).one()
        assert tuple(stored) == (721, None, 'active', None)


def test_orders_and_products(access, review_db):
    from modules.orders.models import Order, Product
    call, owner, status, _ = access
    review_db.add_all([Order(id=i, order_number=str(i), org_id=owner) for i in (721, 722, 723)])
    review_db.add(Product(id=721, name='Bolt', price=5, org_id=owner))
    review_db.execute(text("INSERT INTO models(id,name) VALUES(721,'Bolt')"))
    review_db.execute(text("INSERT INTO consumables(id,name,current_stock,status) VALUES(721,'Box',10,'active')"))
    review_db.flush()
    review_db.execute(text("INSERT INTO order_items(id,order_id,product_id,quantity,fulfilled_quantity) "
                           "VALUES(721,721,721,1,0),(722,722,721,1,0)"))
    review_db.commit()

    readable = 404 if status == 404 else 200
    call('GET', '/products/721', expect=readable)
    call('GET', '/orders/721', expect=readable)
    call('PATCH', '/products/721', {'description': 'Blue bolt'}, noun='product')
    for link, body in [('components', {'model_id': 721}), ('consumables', {'consumable_id': 721})]:
        added = call('POST', f'/products/721/{link}', body, noun='product')
        link_id = added.json()['id'] if status == 200 else 1
        call('DELETE', f'/products/721/{link}/{link_id}', noun='product')
    call('PATCH', '/orders/721', {'notes': 'Packed'}, noun='order', admin_only=True)
    call('POST', '/orders/721/items', {'product_id': 721}, noun='order')
    call('PATCH', '/orders/721/items/721', {'unit_price': 4}, noun='order', admin_only=True)
    call('DELETE', '/orders/721/items/721', noun='order')
    call('POST', '/orders/721/schedule', noun='order')
    call('POST', '/orders/722/items/722/confirm-production', {'quantity': 1}, noun='order')
    call('PATCH', '/orders/722/ship', {}, noun='order')
    call('DELETE', '/orders/723', noun='order')
    # A separate product has no order history that would prevent deletion.
    review_db.add(Product(id=724, name='Washer', org_id=owner))
    review_db.commit()
    call('DELETE', '/products/724', noun='product', admin_only=True)


def test_archive_project_and_printer_history(access, review_db):
    call, owner, status, platform = access
    review_db.execute(text("INSERT INTO printers(id,name,org_id,is_active,gcode_state) "
                           "VALUES(720,'Bench printer',:owner,TRUE,'RUNNING')"), {'owner': owner})
    review_db.execute(text("INSERT INTO projects(id,name,org_id) VALUES(721,'Service run',:owner)"), {'owner': owner})
    review_db.execute(text("INSERT INTO print_archives(id,printer_id,print_name,status,ownership_state,org_id) "
                           "VALUES(721,720,'Sample','completed','known',:owner)"), {'owner': owner})
    review_db.execute(text("INSERT INTO maintenance_logs(id,printer_id,task_name) VALUES(721,720,'Inspection')"))
    review_db.execute(text("INSERT INTO print_jobs(id,printer_id,job_name,status,started_at) VALUES(721,720,'Sample','running',CURRENT_TIMESTAMP)"))
    review_db.execute(text("INSERT INTO vision_detections(id,printer_id,print_job_id,detection_type,confidence,status) "
                           "VALUES(721,720,721,'spaghetti',0.9,'pending')"))
    review_db.commit()
    readable = 404 if status == 404 else 200
    call('GET', '/projects/721', expect=readable)
    call('GET', '/archives/721', expect=readable)
    logs = call('GET', '/maintenance/logs?printer_id=720', expect=200)
    assert any(row['id'] == 721 for row in logs.json()) == (readable == 200)
    call('GET', '/vision/detections/721', expect=200 if platform else 404)
    call('POST', '/maintenance/logs', {'printer_id': 720, 'task_name': 'Cleaned'}, expect=readable)
    call('DELETE', '/maintenance/logs/721', noun='printer', admin_only=True)
    call('POST', '/vision/detections/721/dismiss-and-resume', expect=200 if platform else 404)
    call('PUT', '/projects/721', {'name': 'Sample run'}, noun='project')
    call('POST', '/projects/721/archives', {'archive_ids': [721]}, noun='project')
    call('PATCH', '/archives/721', {'notes': 'Checked'}, noun='archive')
    call('PATCH', '/archives/721/tags', {'tags': ['checked']}, noun='archive')
    call('DELETE', '/archives/721', noun='archive', admin_only=True)
    call('DELETE', '/projects/721', noun='project', admin_only=True)


def test_partial_bulk_writes(routes, review_db):
    from modules.inventory.models import FilamentLibrary, Spool
    client, as_user = routes
    headers = as_user('admin')
    review_db.add(FilamentLibrary(id=721, brand='Fixture', name='PLA'))
    review_db.flush()
    for identifier, owner in [(721, 1), (722, None), (723, 2)]:
        review_db.add(Spool(id=identifier, filament_id=721, org_id=owner))
        review_db.execute(text("INSERT INTO print_archives(id,print_name,status,ownership_state,org_id,tags) "
                               "VALUES(:id,'Part','completed','known',:owner,'blue, red')"),
                          {'id': identifier, 'owner': owner})
    review_db.execute(text("INSERT INTO projects(id,name,org_id) VALUES(721,'Owned',1)"))
    review_db.commit()
    for path, body, count_key, noun in [
        ('/spools/bulk-update', {'spool_ids': [722, 721, 722], 'action': 'archive'}, 'affected', 'spool'),
        ('/projects/721/archives', {'archive_ids': [722, 721, 722]}, 'assigned', 'archive'),
        ('/tags/rename', {'old': 'blue', 'new': 'green'}, 'updated', 'archive'),
        ('/tags/red', None, 'updated', 'archive'),
    ]:
        response = client.request('DELETE' if body is None else 'POST', path, json=body, headers=headers)
        check(response, 200)
        assert response.json()[count_key] == 1
        assert response.json()['skipped'] == [{f'{noun}_id': 722, 'detail': detail(noun)}]
    assert review_db.execute(text('SELECT status FROM spools WHERE id>=721 ORDER BY id')).scalars().all() == [
        'archived', 'active', 'active']
    assert review_db.execute(text('SELECT project_id,tags FROM print_archives WHERE id>=721 ORDER BY id')).all() == [
        (721, 'green'), (None, 'blue, red'), (None, 'blue, red')]


def test_bulk_spool_update_selects_without_locks(routes, review_db):
    from unittest.mock import patch
    from sqlalchemy.orm import Query
    from modules.inventory.models import FilamentLibrary, Spool
    from modules.inventory.routes import spool_ops

    client, as_user = routes
    review_db.add(FilamentLibrary(id=721, brand='Fixture', name='PLA'))
    review_db.flush()
    review_db.add_all([Spool(id=721, filament_id=721, org_id=1), Spool(id=722, filament_id=721)])
    review_db.commit()
    with patch.object(spool_ops, 'lock_slot_graph', wraps=spool_ops.lock_slot_graph) as graph_lock, \
            patch.object(Query, 'with_for_update', autospec=True, side_effect=Query.with_for_update) as row_lock:
        result = client.post('/spools/bulk-update', json={'spool_ids': [721, 722], 'action': 'archive'},
                             headers=as_user('operator1'))
    check(result, 200)
    assert result.json() == {'status': 'ok', 'affected': 1, 'skipped': [{'spool_id': 722, 'detail': detail('spool')}]}
    assert (graph_lock.call_count, row_lock.call_count) == (0, 0)


@pytest.mark.parametrize('source,target', [(721, 722), (722, 721)])
def test_merge_checks_both_spools(routes, review_db, source, target):
    from modules.inventory.models import FilamentLibrary, Spool
    client, as_user = routes
    review_db.add(FilamentLibrary(id=721, brand='Bambu Lab', name='PLA'))
    review_db.flush()
    review_db.add_all([Spool(id=721, filament_id=721, org_id=1), Spool(id=722, filament_id=721)])
    review_db.commit()
    result = client.post(f'/spools/rfid-onboarding/{source}/merge', json={'sealed_spool_id': target},
                         headers=as_user('operator1'))
    check(result, 403, 'spool')
    assert review_db.execute(text('SELECT status FROM spools WHERE id IN (721,722)')).scalars().all() == [
        'active', 'active']


def test_member_can_create_with_unscoped_references(routes, review_db):
    client, as_user = routes
    review_db.execute(text("INSERT INTO models(id,name) VALUES(721,'Bracket')"))
    review_db.execute(text("INSERT INTO products(id,name,price) VALUES(721,'Bracket',10)"))
    review_db.commit()
    headers = as_user('operator1')
    product = client.post('/products', json={'name': 'Pair', 'components': [{'model_id': 721}]}, headers=headers)
    check(product, 200)
    order = client.post('/orders', json={'items': [{'product_id': 721}]}, headers=headers)
    check(order, 200)
    assert review_db.execute(text('SELECT org_id FROM products WHERE id=:id'),
                             {'id': product.json()['id']}).scalar_one() == 1
    assert review_db.execute(text('SELECT org_id FROM orders WHERE id=:id'),
                             {'id': order.json()['id']}).scalar_one() == 1


def test_member_reprints_visible_unscoped_archive(reprint_client, review_db):
    client, _, _ = reprint_client
    review_db.execute(text('UPDATE print_archives SET org_id=NULL WHERE id=101'))
    review_db.commit()
    response = client.post('/archives/101/reprint', json={'printer_id': 9})
    check(response, 200)
    assert review_db.execute(text('SELECT charged_to_org_id FROM jobs WHERE id=:id'),
                             {'id': response.json()['job_id']}).scalar_one() == 1


@pytest.mark.postgresql
def test_native_bulk_spool_and_archive_writes(pg_engine):
    import asyncio
    from sqlalchemy.orm import Session
    from core.schema import bootstrap_database
    from modules.archives.routes.projects import BulkAssign, assign_archives
    from modules.archives.routes.tags import TagRename, rename_tag
    from modules.inventory.routes.spool_ops import bulk_update_spools

    bootstrap_database(pg_engine)
    actor = {'role': 'admin', 'group_id': 1, 'id': 3}
    with Session(pg_engine) as db:
        db.execute(text("INSERT INTO groups(id,name,is_org) VALUES(1,'Shop',TRUE)"))
        db.execute(text("INSERT INTO projects(id,name,org_id) VALUES(721,'Parts',1)"))
        db.execute(text("INSERT INTO print_archives(id,print_name,status,ownership_state,org_id,tags) "
                        "VALUES(721,'Bolt','completed','known',1,'blue'),(722,'Nut','completed','known',NULL,'blue')"))
        db.execute(text("INSERT INTO filament_library(id,brand,name) VALUES(721,'Test','PLA')"))
        db.execute(text("INSERT INTO spools(id,filament_id,status,org_id) "
                        "VALUES(721,721,'active',1),(722,721,'active',NULL)"))
        db.commit()
        skipped = [{'archive_id': 722, 'detail': detail('archive')}]
        assert assign_archives(721, BulkAssign(archive_ids=[721, 722]), actor, db) == {
            'assigned': 1, 'skipped': skipped}
        assert rename_tag(TagRename(old='blue', new='red'), actor, db) == {'updated': 1, 'skipped': skipped}
        assert asyncio.run(bulk_update_spools({'spool_ids': [721, 722], 'action': 'archive'}, actor, db)) == {
            'status': 'ok', 'affected': 1, 'skipped': [{'spool_id': 722, 'detail': detail('spool')}]}


def test_catalogue_offers_only_writable_spool_changes(routes, review_db):
    from modules.inventory.catalogue_slots import slot_catalogue
    from modules.inventory.models import FilamentLibrary, Spool
    from modules.printers.models import FilamentSlot, Printer
    client, as_user = routes
    for identifier, name in [(721, 'Blue PLA'), (722, 'Blue Silk')]:
        review_db.add(FilamentLibrary(id=identifier, brand='Test', name=name, material='PLA',
                                      color_hex='ABCDEF', catalogue_code=name))
    review_db.add(FilamentLibrary(id=723, brand='Test', name='Unlabelled', material='PLA'))
    review_db.add(Printer(id=720, name='Bench printer'))
    review_db.flush()
    review_db.add(Spool(id=720, filament_id=723))
    review_db.flush()
    slot = FilamentSlot(printer_id=720, slot_number=1, filament_type='PLA', color_hex='ABCDEF',
                        assigned_spool_id=720)
    review_db.add(slot)
    review_db.commit()
    member = {'id': 6, 'role': 'operator', 'group_id': 1}
    assert slot_catalogue(review_db, slot, member)['catalogue_choices'] == []
    assert len(slot_catalogue(review_db, slot, dict(member, group_id=None))['catalogue_choices']) == 2
    review_db.get(Spool, 720).filament_id = 721
    review_db.commit()
    assert slot_catalogue(review_db, slot, member)['catalogue_name'] == 'Blue PLA'
    check(client.post('/printers/720/slots/1/catalogue', headers=as_user('operator1'),
                      json={'spool_id': 720, 'filament_id': 721}), 200)
