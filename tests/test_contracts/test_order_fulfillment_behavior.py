"""Advertised order fulfilment follows actual completion of all generated jobs."""
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_product_bom_integrity import bom_client
from tests.test_contracts.test_order_production_behavior import production_order


def test_order_becomes_fulfilled_when_all_generated_jobs_complete(production_order,review_db,monkeypatch):
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.jobs.routes import jobs_lifecycle, jobs_crud
    monkeypatch.setitem(registry._providers,'EducationPolicyProvider',EducationPolicyService())
    client,identity,order_id=production_order
    client.app.include_router(jobs_lifecycle.router)
    client.app.include_router(jobs_crud.router)
    response=client.post(f'/orders/{order_id}/schedule');assert response.status_code==200,response.text
    with Session(review_db.get_bind()) as fresh:
        ids=list(fresh.execute(text('SELECT id FROM jobs WHERE order_item_id IN (SELECT id FROM order_items WHERE order_id=:id) ORDER BY id'),{'id':order_id}).scalars())
    assert len(ids)==3
    first=client.post(f'/jobs/{ids[0]}/complete');assert first.status_code==200,first.text
    response=client.get(f'/orders/{order_id}');assert response.status_code==200,response.text
    assert response.json()['status']!='fulfilled'
    assert response.json()['jobs_complete']==1
    for identifier in ids[1:]:
        response=client.post(f'/jobs/{identifier}/complete');assert response.status_code==200,response.text
    response=client.get(f'/orders/{order_id}');assert response.status_code==200,response.text
    assert response.json()['status']=='fulfilled',response.text
    assert response.json()['fulfilled_items']==3 and response.json()['items'][0]['fulfilled_quantity']==3
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT status FROM orders WHERE id=:id'),{'id':order_id}).scalar_one()=='fulfilled'
        assert fresh.execute(text('SELECT fulfilled_quantity FROM order_items WHERE order_id=:id'),{'id':order_id}).scalar_one()==3


@pytest.mark.parametrize("transition", ["reset", "fail", "delete"])
def test_terminal_job_reversal_reduces_production_fulfillment(production_order, review_db, monkeypatch, transition):
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.jobs.routes import jobs_lifecycle, jobs_crud
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    client, identity, order_id = production_order
    client.app.include_router(jobs_lifecycle.router)
    client.app.include_router(jobs_crud.router)
    response = client.post(f'/orders/{order_id}/schedule')
    assert response.status_code == 200, response.text
    with Session(review_db.get_bind()) as fresh:
        ids = list(fresh.execute(text('SELECT id FROM jobs WHERE order_item_id IN (SELECT id FROM order_items WHERE order_id=:id) ORDER BY id'), {'id': order_id}).scalars())
    for identifier in ids:
        response = client.post(f'/jobs/{identifier}/complete')
        assert response.status_code == 200, response.text
    assert client.get(f'/orders/{order_id}').json()['status'] == 'fulfilled'
    response = client.delete(f'/jobs/{ids[0]}') if transition == 'delete' else client.post(f'/jobs/{ids[0]}/{transition}')
    assert response.status_code == (204 if transition == 'delete' else 200), response.text
    result = client.get(f'/orders/{order_id}').json()
    assert result['status'] == 'in_progress'
    assert result['items'][0]['fulfilled_quantity'] == 1  # Five surviving pieces / three per product.
    if transition == 'delete':
        with Session(review_db.get_bind()) as fresh:
            assert fresh.execute(text('SELECT COUNT(*) FROM order_production_jobs WHERE job_id=:id'), {'id': ids[0]}).scalar_one() == 1
        return
    response = client.post(f'/jobs/{ids[0]}/complete')
    assert response.status_code == 200, response.text
    result = client.get(f'/orders/{order_id}').json()
    assert result['status'] == 'fulfilled' and result['items'][0]['fulfilled_quantity'] == 3


@pytest.mark.parametrize('terminal', ['completed', 'failed', 'cancelled', 'provider_unavailable', 'commit_fault_completed', 'commit_fault_failed', 'commit_fault_cancelled'])
def test_raw_monitor_reconciles_original_production(production_order, review_db, tmp_path, monkeypatch, terminal):
    import hashlib
    from contextlib import contextmanager
    from core.db_utils import _ConnectionAdapter
    from core.registry import registry
    from modules.notifications import job_events
    from modules.printers import dispatch
    from tests.test_contracts.dispatch_test_support import configure_dispatch
    client, identity, order_id = production_order
    engine, source = configure_dispatch(review_db,tmp_path,monkeypatch,protocol='prusalink')
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO print_files(id,model_id,filename,original_filename,stored_path,org_id) VALUES(58,101,:name,:name,:path,1)"),{'name':source.name,'path':str(source)})
    response=client.post(f'/orders/{order_id}/schedule')
    assert response.status_code==200,response.text
    with engine.begin() as connection:
        ids=list(connection.execute(text('SELECT id FROM jobs WHERE order_item_id IN (SELECT id FROM order_items WHERE order_id=:id) ORDER BY id'),{'id':order_id}).scalars())
        for identifier in ids:
            connection.execute(text('UPDATE jobs SET printer_id=59,print_file_id=58,payload_sha256=:sha,plate_index=0 WHERE id=:id'),{'sha':hashlib.sha256(source.read_bytes()).hexdigest(),'id':identifier})
    @contextmanager
    def isolated_db():
        connection=engine.raw_connection()
        try:yield _ConnectionAdapter(connection,False)
        except BaseException:
            connection.rollback();raise
        finally:connection.close()
    monkeypatch.setattr(job_events,'get_db',isolated_db)
    monkeypatch.setattr(job_events,'dispatch_alert',lambda **kwargs:None)
    monkeypatch.setattr(job_events,'increment_care_counters',lambda *args:None)
    class QuietBus:
        def publish(self,event):pass
    monkeypatch.setattr(job_events,'get_event_bus',lambda:QuietBus())
    names=[]
    monkeypatch.setattr(dispatch,'_dispatch_prusalink',lambda jid,path,name,credentials,**kwargs:(names.append(name) or True,'Owned physical sender stub'))
    attempts=[]
    fault_commit=terminal.startswith('commit_fault_')
    if fault_commit:terminal=terminal.removeprefix('commit_fault_')
    for index,identifier in enumerate(ids):
        dispatched = dispatch.dispatch_job(59,identifier)
        assert dispatched[0], dispatched[1]
        attempt=job_events.job_started(59,names[-1]);assert attempt is not None
        attempts.append(attempt)
        with engine.connect() as connection:
            assert connection.execute(text('SELECT status,scheduled_job_id FROM print_jobs WHERE id=:id'),{'id':attempt}).one()==('running',identifier)
            assert connection.execute(text('SELECT observed_print_job_id FROM dispatch_attempt_receipts WHERE job_id=:id'),{'id':identifier}).scalar_one()==attempt
            original_start = connection.execute(text('SELECT actual_start FROM jobs WHERE id=:id'),{'id':identifier}).scalar_one()
        if index==0 and fault_commit:
            with engine.begin() as connection:
                connection.exec_driver_sql('CREATE TABLE owned_terminal_fk(value INTEGER REFERENCES users(id) DEFERRABLE INITIALLY DEFERRED)')
                connection.exec_driver_sql("CREATE TRIGGER owned_terminal_commit_fault AFTER UPDATE ON print_jobs WHEN OLD.status='running' AND NEW.status IN ('completed','failed','cancelled') BEGIN INSERT INTO owned_terminal_fk VALUES(2147483647); END")
            if terminal=='cancelled':job_events.job_cancelled(59,attempt)
            else:job_events.job_completed(59,attempt,success=terminal=='completed')
            with engine.connect() as connection:
                assert connection.execute(text('SELECT status,ended_at FROM print_jobs WHERE id=:id'),{'id':attempt}).one()==('running',None)
                assert connection.execute(text('SELECT status,actual_start,actual_end FROM jobs WHERE id=:id'),{'id':identifier}).one()==('printing',original_start,None)
                assert connection.execute(text('SELECT count(*) FROM printer_dispatch_claims WHERE printer_id=59')).scalar_one()==1
                assert connection.execute(text('SELECT count(*) FROM owned_terminal_fk')).scalar_one()==0
            with engine.begin() as connection:
                connection.exec_driver_sql('DROP TRIGGER owned_terminal_commit_fault')
                connection.exec_driver_sql('DROP TABLE owned_terminal_fk')
        if index==0 and terminal=='provider_unavailable':
            provider=registry.get_provider('OrderFulfillmentProvider')
            monkeypatch.delitem(registry._providers,'OrderFulfillmentProvider')
            job_events.job_completed(59,attempt,success=True)
            with engine.connect() as connection:
                assert connection.execute(text('SELECT status FROM print_jobs WHERE id=:id'),{'id':attempt}).scalar_one()=='running'
            monkeypatch.setitem(registry._providers,'OrderFulfillmentProvider',provider)
        if index==0 and terminal=='cancelled':job_events.job_cancelled(59,attempt)
        else:job_events.job_completed(59,attempt,success=not(index==0 and terminal=='failed'))
        with engine.connect() as connection:
            physical_end = connection.execute(text('SELECT ended_at FROM print_jobs WHERE id=:id'),{'id':attempt}).scalar_one()
            job_times = connection.execute(text('SELECT actual_start,actual_end FROM jobs WHERE id=:id'),{'id':identifier}).one()
            assert physical_end is not None and job_times==(original_start,physical_end)
    result=client.get(f'/orders/{order_id}').json()
    complete=terminal in ('completed','provider_unavailable')
    assert result['status']==('fulfilled' if complete else 'in_progress')
    assert result['items'][0]['fulfilled_quantity']==(3 if complete else 1)
    if complete:
        job_events.job_completed(59,attempts[0],success=False)
        job_events.job_cancelled(59,attempts[0])
        with engine.connect() as connection:
            assert connection.execute(text('SELECT status FROM print_jobs WHERE id=:id'),{'id':attempts[0]}).scalar_one()=='completed'
        assert client.get(f'/orders/{order_id}').json()['items'][0]['fulfilled_quantity']==3


@pytest.mark.parametrize('handler', ['mqtt', 'moonraker'])
@pytest.mark.parametrize('terminal', ['completed', 'failed'])
def test_direct_printer_monitor_fulfills_original_jobs(production_order, review_db, monkeypatch, handler, terminal):
    from contextlib import contextmanager
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.notifications import job_events
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    client, identity, order_id = production_order
    response = client.post(f'/orders/{order_id}/schedule')
    assert response.status_code == 200, response.text
    engine = review_db.get_bind()
    with engine.begin() as connection:
        ids = list(connection.execute(text('SELECT id FROM jobs WHERE order_item_id IN (SELECT id FROM order_items WHERE order_id=:id) ORDER BY id'), {'id': order_id}).scalars())
        for index, identifier in enumerate(ids):
            connection.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,job_name,status,started_at) VALUES(:id,9,:job,'Synthetic production','printing',CURRENT_TIMESTAMP)"), {'id': 700 + index, 'job': identifier})

    @contextmanager
    def isolated_db():
        connection = engine.raw_connection()
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
    monkeypatch.setattr(job_events, 'get_db', isolated_db)
    monkeypatch.setattr(job_events, 'dispatch_alert', lambda **kwargs: None)
    monkeypatch.setattr(job_events, 'increment_care_counters', lambda *args: None)
    class QuietBus:
        def publish(self, event):
            pass
    monkeypatch.setattr(job_events, 'get_event_bus', lambda: QuietBus())
    from types import SimpleNamespace
    from modules.notifications import alert_dispatch, event_dispatcher
    from modules.archives import archive
    monkeypatch.setattr(alert_dispatch, 'dispatch_alert', lambda **kwargs: None)
    monkeypatch.setattr(alert_dispatch, '_start_bed_cooled_monitor', lambda *args: None)
    monkeypatch.setattr(event_dispatcher, 'dispatch_alert', lambda **kwargs: None)
    monkeypatch.setattr(event_dispatcher, 'record_error', lambda **kwargs: None)
    monkeypatch.setattr(event_dispatcher, 'increment_care_counters', lambda *args: None)
    monkeypatch.setattr(archive, 'create_print_archive', lambda **kwargs: None)
    if handler == 'mqtt':
        from modules.printers.monitors import mqtt_job_lifecycle as monitor
        monkeypatch.setattr(monitor, 'get_db', isolated_db)
        def finish(index, outcome):
            return monitor.record_job_ended(9, 'Synthetic printer', 700 + index, ids[index], outcome,
                                            {'subtask_name': 'Synthetic production'}, {}, lambda **kwargs: None)
    else:
        from modules.printers.monitors import moonraker_monitor as monitor
        monkeypatch.setattr(monitor, 'engine', engine)
        # Construct a monitor without connecting, and exercise its actual terminal transaction.
        instance = monitor.MoonrakerMonitor.__new__(monitor.MoonrakerMonitor)
        instance.printer_id = 9
        instance.name = 'Synthetic printer'
        instance._last_filename = 'Synthetic production'
        def finish(index, outcome):
            instance._current_job_db_id = 700 + index
            return instance._job_ended(outcome, SimpleNamespace(filename='', filament_used_mm=0, error_message=''))
    for index in range(1, len(ids)):
        finish(index, 'completed')
    finish(0, terminal)
    result = client.get(f'/orders/{order_id}').json()
    assert result['status'] == ('fulfilled' if terminal == 'completed' else 'in_progress')
    assert result['items'][0]['fulfilled_quantity'] == (3 if terminal == 'completed' else 1)
    with engine.connect() as connection:
        assert connection.execute(text('SELECT status FROM jobs WHERE id=:id'), {'id': ids[0]}).scalar_one() == terminal


@pytest.mark.parametrize('adapter', ['session', 'connection', 'raw'])
def test_anchor_corruption_refused_and_missing_provider_rejects_original_binding(production_order, review_db, monkeypatch, adapter):
    from fastapi import HTTPException
    from core.registry import registry
    from core.order_production import prepare_job_write
    client, identity, order_id = production_order
    assert client.post(f'/orders/{order_id}/schedule').status_code == 200
    engine = review_db.get_bind()
    with engine.begin() as connection:
        identifier = connection.execute(text('SELECT job_id FROM order_production_jobs ORDER BY job_id LIMIT 1')).scalar_one()
        from sqlalchemy.exc import IntegrityError
        with pytest.raises(IntegrityError,match='economic source anchor differs'):
            connection.execute(text('UPDATE jobs SET order_item_id=NULL WHERE id=:id'), {'id': identifier})
        assert connection.execute(text('SELECT order_item_id FROM jobs WHERE id=:id'),{'id':identifier}).scalar_one() is not None
    monkeypatch.delitem(registry._providers, 'OrderFulfillmentProvider')
    executor = Session(engine) if adapter == 'session' else engine.connect() if adapter == 'connection' else engine.raw_connection()
    try:
        with pytest.raises(HTTPException) as failure:
            prepare_job_write(executor, [identifier])
        assert failure.value.status_code == 503
    finally:
        executor.rollback()
        executor.close()


@pytest.mark.parametrize('change', ['tenant', 'delete'])
def test_job_completion_rechecks_row_after_parent_lock(production_order, review_db, monkeypatch, change):
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.jobs.routes import jobs_lifecycle
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    client, identity, order_id = production_order
    client.app.include_router(jobs_lifecycle.router)
    assert client.post(f'/orders/{order_id}/schedule').status_code == 200
    with review_db.get_bind().connect() as connection:
        identifier = connection.execute(text('SELECT job_id FROM order_production_jobs ORDER BY job_id LIMIT 1')).scalar_one()
    original = jobs_lifecycle.prepare_job_write
    def change_locked_row(session, identifiers, *args, **kwargs):
        parents = original(session, identifiers, *args, **kwargs)
        statement = 'UPDATE jobs SET charged_to_org_id=2 WHERE id=:id' if change == 'tenant' else 'DELETE FROM jobs WHERE id=:id'
        session.execute(text(statement), {'id': identifier})
        return parents
    monkeypatch.setattr(jobs_lifecycle, 'prepare_job_write', change_locked_row)
    if change == 'tenant':
        from sqlalchemy.exc import IntegrityError
        with pytest.raises(IntegrityError,match='economic source anchor differs'):
            client.post(f'/jobs/{identifier}/complete')
    else:
        response = client.post(f'/jobs/{identifier}/complete')
        assert response.status_code == 404, response.text
    with review_db.get_bind().connect() as connection:
        row = connection.execute(text('SELECT status,charged_to_org_id FROM jobs WHERE id=:id'), {'id': identifier}).one()
        assert row == ('pending', 1)  # Rejected transition and injected edit both rolled back.
        assert connection.execute(text('SELECT fulfilled_quantity FROM order_items WHERE order_id=:id'), {'id': order_id}).scalar_one() == 0


@pytest.mark.parametrize('kind', ['consumable_only', 'historical'])
def test_explicit_confirmation_is_bounded_audited_and_idempotent(production_order, review_db, kind):
    client, identity, order_id = production_order
    if kind == 'consumable_only':
        review_db.execute(text('DELETE FROM product_components WHERE product_id=101'))
        review_db.commit()
        assert client.post(f'/orders/{order_id}/schedule').status_code == 200
    detail = client.get(f'/orders/{order_id}').json()
    item = detail['items'][0]
    assert item['can_confirm_production']
    path = f"/orders/{order_id}/items/{item['id']}/confirm-production"
    for quantity, fulfilled in [(1, 1), (1, 1), (3, 3), (0, 0)]:
        response = client.post(path, json={'quantity': quantity})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['items'][0]['fulfilled_quantity'] == fulfilled
        assert result['status'] == ('fulfilled' if fulfilled == 3 else 'in_progress')
    with review_db.get_bind().connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='order.production_confirmed'")).scalar_one() == 3
        assert connection.execute(text('SELECT mode FROM order_production_items WHERE order_id=:id'), {'id': order_id}).scalar_one() == kind
        assert connection.execute(text('SELECT COUNT(*) FROM order_production_jobs')).scalar_one() == 0
    for invalid in [-1, 4, True, 1.5, '2']:
        response = client.post(path, json={'quantity': invalid})
        assert response.status_code in (400, 422), response.text
    assert client.post(f'/orders/{order_id}/items/99999/confirm-production', json={'quantity': 1}).status_code == 404


def test_printed_production_cannot_be_manually_confirmed(production_order):
    client, identity, order_id = production_order
    assert client.post(f'/orders/{order_id}/schedule').status_code == 200
    result = client.get(f'/orders/{order_id}').json()
    item = result['items'][0]
    assert item['production_mode'] == 'mixed' and not item['can_confirm_production']
    response = client.post(f"/orders/{order_id}/items/{item['id']}/confirm-production", json={'quantity': 3})
    assert response.status_code == 409
    identity['role'] = 'admin'
    assert client.patch(f'/orders/{order_id}', json={'status': 'fulfilled'}).status_code == 409
    assert client.get(f'/orders/{order_id}').json()['items'][0]['fulfilled_quantity'] == 0


@pytest.mark.parametrize('status', ['cancelled', 'shipped'])
def test_terminal_order_rejects_manual_confirmation(production_order, review_db, status):
    client, identity, order_id = production_order
    item = client.get(f'/orders/{order_id}').json()['items'][0]
    review_db.execute(text('UPDATE orders SET status=:status WHERE id=:id'), {'status': status, 'id': order_id})
    review_db.commit()
    response = client.post(f"/orders/{order_id}/items/{item['id']}/confirm-production", json={'quantity': 3})
    assert response.status_code == 409, response.text
    result = client.get(f'/orders/{order_id}').json()
    assert not result['items'][0]['can_confirm_production']
    assert result['status'] == status and result['items'][0]['fulfilled_quantity'] == 0


@pytest.mark.parametrize('role', ['viewer', 'foreign'])
def test_confirmation_enforces_role_and_tenant(production_order, role):
    client, identity, order_id = production_order
    item = client.get(f'/orders/{order_id}').json()['items'][0]
    if role == 'viewer':
        identity['role'] = 'viewer'
    else:
        identity['group_id'] = 2
    response = client.post(f"/orders/{order_id}/items/{item['id']}/confirm-production", json={'quantity': 3})
    assert response.status_code == (403 if role == 'viewer' else 404), response.text


@pytest.mark.parametrize('terminal', ['completed', 'cancelled'])
@pytest.mark.parametrize('race', [False, True])
@pytest.mark.parametrize('already_linked', [False, True])
def test_link_completed_print_reconciles_without_stealing_original_job(production_order, review_db, monkeypatch, already_linked, terminal, race):
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.jobs.routes import jobs_lifecycle
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    client, identity, order_id = production_order
    client.app.include_router(jobs_lifecycle.router)
    assert client.post(f'/orders/{order_id}/schedule').status_code == 200
    engine = review_db.get_bind()
    with engine.begin() as connection:
        ids = list(connection.execute(text('SELECT job_id FROM order_production_jobs ORDER BY job_id')).scalars())
        connection.execute(text('UPDATE jobs SET printer_id=9 WHERE id=:id'), {'id': ids[-1]})
        connection.execute(text("INSERT INTO print_jobs(id,printer_id,job_name,status,started_at,scheduled_job_id) VALUES(800,9,'Synthetic completed print',:status,CURRENT_TIMESTAMP,:linked)"), {'linked': ids[0] if already_linked else None, 'status': 'printing' if race else terminal})
    for identifier in ids[:-1]:
        response = client.post(f'/jobs/{identifier}/complete')
        assert response.status_code == 200, response.text
    original_execute = Session.execute
    raced = []
    def finish_before_link_write(session, statement, *args, **kwargs):
        if race and str(statement).startswith('UPDATE print_jobs SET scheduled_job_id'):
            original_execute(session, text('UPDATE print_jobs SET status=:status WHERE id=800'), {'status': terminal})
            raced.append(True)
        return original_execute(session, statement, *args, **kwargs)
    monkeypatch.setattr(Session, 'execute', finish_before_link_write)
    response = client.post(f'/jobs/{ids[-1]}/link-print', params={'print_job_id': 800})
    assert response.status_code == (409 if already_linked else 200), response.text
    result = client.get(f'/orders/{order_id}').json()
    assert result['status'] == ('fulfilled' if not already_linked and terminal == 'completed' else 'in_progress')
    assert result['items'][0]['fulfilled_quantity'] == (3 if not already_linked and terminal == 'completed' else 2)
    with engine.connect() as connection:
        assert connection.execute(text('SELECT scheduled_job_id FROM print_jobs WHERE id=800')).scalar_one() == (ids[0] if already_linked else ids[-1])
    if race and not already_linked:
        assert raced == [True]
    if not already_linked:
        with engine.connect() as connection:
            assert connection.execute(text('SELECT status FROM jobs WHERE id=:id'), {'id': ids[-1]}).scalar_one() == terminal


def test_bulk_cancel_counts_actual_changes_and_retains_production_bindings(production_order, review_db, monkeypatch):
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.jobs.routes import jobs_lifecycle, jobs_crud
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    client, identity, order_id = production_order
    client.app.include_router(jobs_lifecycle.router)
    client.app.include_router(jobs_crud.router)
    assert client.post(f'/orders/{order_id}/schedule').status_code == 200
    engine = review_db.get_bind()
    with engine.connect() as connection:
        ids = list(connection.execute(text('SELECT job_id FROM order_production_jobs ORDER BY job_id')).scalars())
    assert client.post(f'/jobs/{ids[0]}/complete').status_code == 200
    response = client.post('/jobs/bulk-update', json={'job_ids': ids + ids + [99999], 'action': 'cancel'})
    assert response.status_code == 200, response.text
    assert response.json()['affected'] == 2  # Completed/missing/duplicate jobs are not changes.
    result = client.get(f'/orders/{order_id}').json()
    assert result['status'] == 'in_progress' and result['items'][0]['fulfilled_quantity'] == 1
    response = client.post('/jobs/bulk-update', json={'job_ids': ids, 'action': 'cancel'})
    assert response.status_code == 200 and response.json()['affected'] == 0
    response = client.post('/jobs/bulk-update', json={'job_ids': ids, 'action': 'delete'})
    assert response.status_code == 200 and response.json()['affected'] == 2
    with engine.connect() as connection:
        assert connection.execute(text('SELECT COUNT(*) FROM order_production_jobs')).scalar_one() == 3
        assert connection.execute(text('SELECT COUNT(*) FROM jobs WHERE id IN (:one,:two,:three)'), dict(zip(['one', 'two', 'three'], ids))).scalar_one() == 1
    for bad in [[True], ['1'], [0], [-1], [None]]:
        assert client.post('/jobs/bulk-update', json={'job_ids': bad, 'action': 'delete'}).status_code == 400
    monkeypatch.setattr(jobs_crud, 'prepare_job_write', lambda *args, **kwargs: pytest.fail('Invalid request reached parent locking'))
    for priority in [True, False, 1.0, '1', None, 0, 6]:
        response = client.post('/jobs/bulk-update', json={'job_ids': [ids[0]], 'action': 'set_priority', 'priority': priority})
        assert response.status_code == 400, response.text
    assert client.post('/jobs/bulk-update', json={'job_ids': [ids[0]], 'action': 'unknown'}).status_code == 400


def test_bulk_priority_and_hold_count_changes_not_unchanged_matches(production_order, review_db, monkeypatch):
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    from modules.jobs.routes import jobs_crud
    client, identity, order_id = production_order
    client.app.include_router(jobs_crud.router)
    assert client.post(f'/orders/{order_id}/schedule').status_code == 200
    with review_db.get_bind().connect() as connection:
        ids = list(connection.execute(text('SELECT job_id FROM order_production_jobs ORDER BY job_id')).scalars())
    assert len(ids) == 3
    for action, fields in [('set_priority', {'priority': 5}), ('hold', {}), ('unhold', {})]:
        body = {'job_ids': ids + ids, 'action': action, **fields}
        first = client.post('/jobs/bulk-update', json=body)
        assert first.status_code == 200 and first.json()['affected'] == 3, first.text
        repeated = client.post('/jobs/bulk-update', json=body)
        assert repeated.status_code == 200 and repeated.json()['affected'] == 0, repeated.text


def test_unknown_original_job_quantity_is_not_invented_as_completed_production(production_order, review_db, monkeypatch):
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.jobs.routes import jobs_lifecycle, jobs_crud
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    client, identity, order_id = production_order
    client.app.include_router(jobs_lifecycle.router)
    client.app.include_router(jobs_crud.router)
    assert client.post(f'/orders/{order_id}/schedule').status_code == 200
    engine = review_db.get_bind()
    with engine.begin() as connection:
        ids = list(connection.execute(text('SELECT job_id FROM order_production_jobs ORDER BY job_id')).scalars())
        connection.execute(text('UPDATE jobs SET quantity=NULL WHERE id=:id'), {'id': ids[0]})
    for identifier in ids:
        response = client.post(f'/jobs/{identifier}/complete')
        assert response.status_code == 200, response.text
    result = client.get(f'/orders/{order_id}').json()
    assert result['status'] == 'in_progress' and result['items'][0]['fulfilled_quantity'] == 1
    assert client.get(f'/jobs/{ids[0]}').json()['quantity'] is None
    response = client.patch(f'/jobs/{ids[0]}', json={'quantity': 1})
    assert response.status_code == 200, response.text
    result = client.get(f'/orders/{order_id}').json()
    assert result['status'] == 'fulfilled' and result['items'][0]['fulfilled_quantity'] == 3


@pytest.mark.parametrize('action', ['start', 'move'])
def test_manual_transition_preserves_completion_committed_before_parent_lock(production_order, review_db, monkeypatch, action):
    from datetime import datetime, timezone, timedelta
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.jobs.routes import jobs_lifecycle
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    client, identity, order_id = production_order
    client.app.include_router(jobs_lifecycle.router)
    assert client.post(f'/orders/{order_id}/schedule').status_code == 200
    with review_db.get_bind().connect() as connection:
        identifier = connection.execute(text('SELECT job_id FROM order_production_jobs ORDER BY job_id LIMIT 1')).scalar_one()
    original = jobs_lifecycle.prepare_job_write
    raced = []
    def complete_before_parent_lock(session, identifiers, *args, **kwargs):
        if not raced:
            raced.append(True)
            completed = client.post(f'/jobs/{identifier}/complete')
            assert completed.status_code == 200, completed.text
        return original(session, identifiers, *args, **kwargs)
    monkeypatch.setattr(jobs_lifecycle, 'prepare_job_write', complete_before_parent_lock)
    response = client.post(f'/jobs/{identifier}/start') if action == 'start' else client.patch(f'/jobs/{identifier}/move',
        json={'printer_id': 9, 'scheduled_start': (datetime.now(timezone.utc)+timedelta(days=1)).isoformat()})
    assert response.status_code == (400 if action == 'start' else 409), response.text
    assert raced == [True]
    with review_db.get_bind().connect() as connection:
        assert connection.execute(text('SELECT status FROM jobs WHERE id=:id'), {'id':identifier}).scalar_one() == 'completed'
        assert connection.execute(text('SELECT fulfilled_quantity FROM order_items WHERE order_id=:id'), {'id':order_id}).scalar_one() == 1


def test_manual_move_rejects_foreign_private_printer(production_order, review_db, monkeypatch):
    from datetime import datetime, timezone, timedelta
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.jobs.routes import jobs_lifecycle
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    client, identity, order_id = production_order
    client.app.include_router(jobs_lifecycle.router)
    assert client.post(f'/orders/{order_id}/schedule').status_code == 200
    with review_db.get_bind().begin() as connection:
        identifier = connection.execute(text('SELECT job_id FROM order_production_jobs ORDER BY job_id LIMIT 1')).scalar_one()
        connection.execute(text("INSERT INTO printers(id,name,org_id,shared) VALUES(777,'Synthetic foreign private printer',2,FALSE)"))
    response = client.patch(f'/jobs/{identifier}/move', json={'printer_id':777,
        'scheduled_start':(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()})
    assert response.status_code == 404, response.text
    with review_db.get_bind().connect() as connection:
        assert connection.execute(text('SELECT status,printer_id FROM jobs WHERE id=:id'), {'id':identifier}).one() == ('pending', None)


def test_unequal_components_follow_immutable_bom_after_product_edits(production_order, review_db, monkeypatch):
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.jobs.routes import jobs_lifecycle
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    client, identity, order_id = production_order
    client.app.include_router(jobs_lifecycle.router)
    with review_db.get_bind().begin() as connection:
        connection.execute(text('UPDATE models SET quantity_per_bed=4,build_time_hours=1 WHERE id=103'))
    assert client.post('/products/101/components',json={'model_id':103,'quantity_needed':2}).status_code == 200
    assert client.post(f'/orders/{order_id}/schedule').status_code == 200
    components = client.get('/products/101').json()['components']
    for component in components:
        assert client.delete(f"/products/101/components/{component['id']}").status_code == 204
    assert client.post('/products/101/components',json={'model_id':101,'quantity_needed':999}).status_code == 200
    with review_db.get_bind().connect() as connection:
        rows = connection.execute(text('SELECT job_id,model_id,piece_count FROM order_production_jobs ORDER BY job_id')).all()
    first = [identifier for identifier,model,pieces in rows if model==101]
    second = [identifier for identifier,model,pieces in rows if model==103]
    assert len(first)==3 and len(second)==2
    for identifier in first:
        assert client.post(f'/jobs/{identifier}/complete').status_code == 200
    assert client.get(f'/orders/{order_id}').json()['fulfilled_items'] == 0
    assert client.post(f'/jobs/{second[0]}/complete').status_code == 200
    assert client.get(f'/orders/{order_id}').json()['fulfilled_items'] == 2
    assert client.post(f'/jobs/{second[1]}/complete').status_code == 200
    result = client.get(f'/orders/{order_id}').json()
    assert result['status']=='fulfilled' and result['fulfilled_items']==3
    with review_db.get_bind().connect() as connection:
        assert connection.execute(text('SELECT model_id,pieces_per_unit FROM order_production_components ORDER BY model_id')).all() == [(101,3),(103,2)]


@pytest.mark.parametrize('changes', [{'model_id':103},{'order_item_id':None},{'quantity_on_bed':5},{'quantity':2}])
def test_original_production_identity_cannot_be_rewritten(production_order, review_db, monkeypatch, changes):
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    monkeypatch.setitem(registry._providers,'EducationPolicyProvider',EducationPolicyService())
    client, identity, order_id = production_order
    assert client.post(f'/orders/{order_id}/schedule').status_code == 200
    with review_db.get_bind().connect() as connection:
        original = connection.execute(text('SELECT id,model_id,order_item_id,quantity_on_bed,quantity FROM jobs WHERE order_item_id IS NOT NULL ORDER BY id LIMIT 1')).one()
    response = client.patch(f'/jobs/{original[0]}',json=changes)
    # The generic PATCH does not advertise piece-count or order-link writes;
    # these extra fields are ignored. Editable model/quantity changes reject.
    expected = 409 if set(changes).intersection({'model_id','quantity'}) else 200
    assert response.status_code == expected, response.text
    with review_db.get_bind().connect() as connection:
        assert connection.execute(text('SELECT id,model_id,order_item_id,quantity_on_bed,quantity FROM jobs WHERE id=:id'), {'id':original[0]}).one() == original


@pytest.mark.parametrize('terminal', ['shipped','cancelled'])
def test_production_counts_reconcile_without_reopening_terminal_order(production_order, review_db, monkeypatch, terminal):
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.jobs.routes import jobs_lifecycle
    monkeypatch.setitem(registry._providers,'EducationPolicyProvider',EducationPolicyService())
    client, identity, order_id = production_order
    client.app.include_router(jobs_lifecycle.router)
    assert client.post(f'/orders/{order_id}/schedule').status_code == 200
    identity['role'] = 'admin'  # Cancellation uses the existing admin order-edit policy.
    response = client.patch(f'/orders/{order_id}/ship',json={'tracking_number':'SYNTHETIC'}) if terminal=='shipped' else client.patch(f'/orders/{order_id}',json={'status':'cancelled'})
    assert response.status_code == 200, response.text
    with review_db.get_bind().connect() as connection:
        ids = list(connection.execute(text('SELECT job_id FROM order_production_jobs ORDER BY job_id')).scalars())
    for identifier in ids:
        assert client.post(f'/jobs/{identifier}/complete').status_code == 200
    result = client.get(f'/orders/{order_id}').json()
    assert result['status']==terminal and result['fulfilled_items']==3
    assert client.post(f'/jobs/{ids[0]}/reset').status_code == 200
    result = client.get(f'/orders/{order_id}').json()
    assert result['status']==terminal and result['fulfilled_items']==1
