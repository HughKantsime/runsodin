"""Real persisted background transitions, isolated from physical printer transports."""
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_order_fulfillment_behavior import production_order, review_db, bom_client


@pytest.fixture
def linked_job(production_order, review_db, monkeypatch):
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.jobs.routes import jobs_lifecycle
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    client, identity, order_id = production_order
    client.app.include_router(jobs_lifecycle.router)
    assert client.post(f'/orders/{order_id}/schedule').status_code == 200
    engine = review_db.get_bind()
    with engine.begin() as connection:
        identifier = connection.execute(text('SELECT job_id FROM order_production_jobs ORDER BY job_id LIMIT 1')).scalar_one()
        connection.execute(text("UPDATE jobs SET printer_id=9,status='scheduled' WHERE id=:id"), {'id':identifier})
    return client, engine, order_id, identifier


def assert_completed(engine, order_id, identifier):
    with engine.connect() as connection:
        assert connection.execute(text('SELECT status FROM jobs WHERE id=:id'), {'id':identifier}).scalar_one() == 'completed'
        assert connection.execute(text('SELECT fulfilled_quantity FROM order_items WHERE order_id=:id'), {'id':order_id}).scalar_one() == 1


@pytest.mark.parametrize('race', [False, True])
def test_dispatch_acknowledgment_preserves_newer_completion(linked_job, monkeypatch, tmp_path, race):
    from core import db
    from modules.printers import dispatch
    client, engine, order_id, identifier = linked_job
    payload = tmp_path/'synthetic.gcode'; payload.write_text('; inert fixture\n')
    monkeypatch.setattr(db, 'engine', engine)
    monkeypatch.setattr(dispatch, '_load_job', lambda jid: {
        'id':identifier,'printer_id':9,'status':'scheduled','item_name':'Synthetic production',
        'model_id':101,'stored_path':str(payload),'original_filename':payload.name})
    monkeypatch.setattr(dispatch, '_get_printer_info', lambda pid: {'api_type':'moonraker','ip':'127.0.0.1'})
    monkeypatch.setattr(dispatch, '_ws', lambda *args: None)
    calls = []
    def isolated_transport(*args):
        calls.append(True)
        if race:
            response = client.post(f'/jobs/{identifier}/complete')
            assert response.status_code == 200, response.text
        return True, 'Synthetic peer accepted'
    monkeypatch.setattr(dispatch, '_dispatch_moonraker', isolated_transport)
    success, message = dispatch.dispatch_job(9, identifier)
    assert success, message
    assert calls == [True]
    if race:
        assert_completed(engine, order_id, identifier)
    else:
        with engine.connect() as connection:
            assert connection.execute(text('SELECT status FROM jobs WHERE id=:id'), {'id':identifier}).scalar_one() == 'printing'
            assert connection.execute(text('SELECT fulfilled_quantity FROM order_items WHERE order_id=:id'), {'id':order_id}).scalar_one() == 0


@pytest.mark.parametrize('print_status', ['running', 'printing'])
@pytest.mark.parametrize('race', [False, True])
def test_moonraker_auto_link_does_not_reopen_completed_production(linked_job, race, print_status):
    from modules.printers.monitors.moonraker_monitor import MoonrakerMonitor
    client, engine, order_id, identifier = linked_job
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO print_jobs(id,printer_id,job_name,status,started_at) VALUES(700,9,'Synthetic',:status,CURRENT_TIMESTAMP)"), {'status':print_status})
    instance = MoonrakerMonitor.__new__(MoonrakerMonitor)
    instance.printer_id = 9; instance._current_job_db_id = 700
    if race:
        assert client.post(f'/jobs/{identifier}/complete').status_code == 200
    with engine.begin() as connection:
        instance._link_job(connection, identifier)
    if race:
        assert_completed(engine, order_id, identifier)
    with engine.connect() as connection:
        assert connection.execute(text('SELECT scheduled_job_id FROM print_jobs WHERE id=700')).scalar_one() == (None if race else identifier)
        if not race:
            assert connection.execute(text('SELECT status FROM jobs WHERE id=:id'), {'id':identifier}).scalar_one() == 'printing'


@pytest.mark.parametrize('race', [False, True])
def test_plate_clear_preserves_completion_after_waiting_selection(linked_job, monkeypatch, race):
    from modules.printers import routes_controls
    client, engine, order_id, identifier = linked_job
    client.app.include_router(routes_controls.router)
    with engine.begin() as connection:
        connection.execute(text("UPDATE jobs SET status='waiting_plate_clear' WHERE id=:id"), {'id':identifier})
    original = Session.execute
    raced = []
    def selected_then_completed(session, statement, *args, **kwargs):
        result = original(session, statement, *args, **kwargs)
        if race and not raced and 'SELECT id FROM jobs' in str(statement) and "status = 'waiting_plate_clear'" in str(statement):
            raced.append(True)
            selected = result.fetchone()
            result.close()  # Release SQLite cursor before the other transaction commits.
            class BufferedSelection:
                def fetchone(self):
                    return selected
            result = BufferedSelection()
            # This legacy waiting state is not in the JobStatus ORM enum.
            # Exercise the actual daemon-style terminal transaction, not an
            # unsupported HTTP completion fixture that fails before the race.
            from core.order_production import prepare_job_write, reconcile_job_write
            with engine.begin() as connection:
                parents = prepare_job_write(connection, [identifier])
                connection.execute(text("UPDATE jobs SET status='completed' WHERE id=:id"), {'id':identifier})
                reconcile_job_write(connection, parents)
        return result
    monkeypatch.setattr(Session, 'execute', selected_then_completed)
    response = client.post('/printers/9/plate-cleared')
    assert response.status_code == 200, response.text
    if race:
        assert raced == [True]
        assert_completed(engine, order_id, identifier)
        assert 'released' not in response.json()['message']
    else:
        with engine.connect() as connection:
            assert connection.execute(text('SELECT status FROM jobs WHERE id=:id'), {'id':identifier}).scalar_one() == 'pending'


@pytest.mark.parametrize('operation', ['cleanup', 'assignment'])
def test_scheduler_preserves_completion_after_candidate_selection(linked_job, monkeypatch, operation):
    from datetime import datetime, timezone, timedelta
    from modules.jobs.scheduler import Scheduler
    from modules.jobs.models import Job
    client, engine, order_id, identifier = linked_job
    with engine.begin() as connection:
        connection.execute(text('UPDATE printers SET is_active=FALSE WHERE id!=9'))
        if operation == 'cleanup':
            connection.execute(text('UPDATE jobs SET scheduled_start=:start WHERE id=:id'),
                               {'start':datetime.now(timezone.utc)-timedelta(hours=3),'id':identifier})
        else:
            connection.execute(text("UPDATE jobs SET status='pending' WHERE id=:id"), {'id':identifier})
    from modules.jobs import scheduler as scheduler_module
    scheduler = Scheduler()
    raced = []
    original = scheduler_module.prepare_job_write
    def complete_selected(session, identifiers, *args, **kwargs):
        if identifier in identifiers and not raced:
            raced.append(True)
            response = client.post(f'/jobs/{identifier}/complete')
            assert response.status_code == 200, response.text
        return original(session, identifiers, *args, **kwargs)
    monkeypatch.setattr(scheduler_module, 'prepare_job_write', complete_selected)
    with Session(engine) as session:
        if operation == 'cleanup':
            scheduler._cleanup_stale_schedules(session)
            session.commit()
        else:
            scheduler.run(session, start_date=datetime.now(timezone.utc)+timedelta(days=1))
    assert raced == [True]
    assert_completed(engine, order_id, identifier)


@pytest.mark.parametrize('stock', [100, 1])
def test_moonraker_measured_filament_deduction_is_atomic_and_idempotent(linked_job, monkeypatch, stock):
    from types import SimpleNamespace
    from modules.printers.monitors import moonraker_monitor
    from modules.notifications import event_dispatcher
    from modules.archives import archive
    client, engine, order_id, identifier = linked_job
    monkeypatch.setattr(moonraker_monitor, 'engine', engine)
    alerts = []
    monkeypatch.setattr(event_dispatcher, 'dispatch_alert', lambda **kwargs: alerts.append(kwargs))
    monkeypatch.setattr(event_dispatcher, 'increment_care_counters', lambda *args: None)
    monkeypatch.setattr(archive, 'create_print_archive', lambda **kwargs: None)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO filament_library(id,brand,name) VALUES(700,'Synthetic','Synthetic PLA')"))
        connection.execute(text("INSERT INTO spools(id,filament_id,remaining_weight_g,status,location_printer_id,location_slot,org_id) VALUES(700,700,:stock,'active',9,1,1)"), {'stock':stock})
        connection.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,job_name,status,started_at) VALUES(700,9,:job,'Synthetic','running',CURRENT_TIMESTAMP)"), {'job':identifier})
    status = SimpleNamespace(filename='Synthetic',filament_used_mm=1000,error_message='')
    for _ in range(2):
        instance = moonraker_monitor.MoonrakerMonitor.__new__(moonraker_monitor.MoonrakerMonitor)
        instance.printer_id = 9; instance.name = 'Synthetic'; instance._last_filename = 'Synthetic'
        instance._current_job_db_id = 700
        instance._job_ended('completed', status)
    assert_completed(engine, order_id, identifier)
    assert len(alerts) == 1
    with engine.connect() as connection:
        assert connection.execute(text('SELECT remaining_weight_g FROM spools WHERE id=700')).scalar_one() == pytest.approx(max(0,stock-2.98))
        assert connection.execute(text('SELECT weight_used_g FROM spool_usage WHERE spool_id=700')).scalars().all() == [2.98]


@pytest.mark.parametrize('scenario', ['match', 'window', 'stale', 'adhoc', 'provider_missing'])
def test_mqtt_start_sweep_and_bump_use_atomic_production_boundary(linked_job, monkeypatch, scenario):
    from contextlib import contextmanager
    from datetime import datetime, timezone, timedelta
    from core.registry import registry
    from modules.printers.monitors import mqtt_job_lifecycle
    client, engine, order_id, identifier = linked_job
    with engine.begin() as connection:
        connection.execute(text('UPDATE jobs SET scheduled_start=:start WHERE id=:id'),
                           {'start':datetime.now(timezone.utc)+timedelta(hours=-3 if scenario=='stale' else 4 if scenario=='adhoc' else 0),'id':identifier})
    if scenario == 'provider_missing':
        monkeypatch.delitem(registry._providers, 'OrderFulfillmentProvider')
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
    monkeypatch.setattr(mqtt_job_lifecycle, 'get_db', isolated_db)
    state = {'subtask_name':'Own model' if scenario not in ('adhoc','window') else 'Unmatched synthetic file','total_layer_num':0}
    alerts = []; reschedules = []
    result = mqtt_job_lifecycle.record_job_started(9,'Synthetic',state,
                lambda **kwargs:alerts.append(kwargs), lambda:reschedules.append(True))
    with engine.connect() as connection:
        current = connection.execute(text('SELECT status,printer_id FROM jobs WHERE id=:id'), {'id':identifier}).one()
        if scenario == 'provider_missing':
            assert result[:2] == (None,None)
            assert current == ('scheduled',9)
            assert connection.execute(text('SELECT COUNT(*) FROM print_jobs')).scalar_one() == 0
            assert not alerts and not reschedules
        elif scenario in ('match','window'):
            assert result[0] is not None and result[1] == identifier
            assert current == ('printing',9)
            assert not alerts and not reschedules
        else:
            assert result[0] is not None and result[1] is None
            assert current == ('pending',None)
            assert len(alerts) == len(reschedules) == 1
        assert connection.execute(text('SELECT fulfilled_quantity FROM order_items WHERE order_id=:id'), {'id':order_id}).scalar_one() == 0


def test_scheduler_defers_new_production_added_during_parent_capture(linked_job, monkeypatch):
    from datetime import datetime, timezone, timedelta
    from modules.jobs import scheduler as scheduler_module
    client, engine, order_id, identifier = linked_job
    original = scheduler_module.prepare_job_write
    inserted = []
    def schedule_new_order(session, identifiers, *args, **kwargs):
        if not inserted:
            response = client.post('/orders',json={'order_number':'Synthetic inserted during scheduler','items':[{'product_id':101,'quantity':1}]})
            assert response.status_code == 200, response.text
            new_order = response.json()['id']; inserted.append(new_order)
            assert client.post(f'/orders/{new_order}/schedule').status_code == 200
        return original(session, identifiers, *args, **kwargs)
    monkeypatch.setattr(scheduler_module, 'prepare_job_write', schedule_new_order)
    with Session(engine) as session:
        result = scheduler_module.Scheduler().run(session,start_date=datetime.now(timezone.utc)+timedelta(days=1))
    with engine.connect() as connection:
        rows = connection.execute(text('SELECT j.id,j.status,j.printer_id FROM jobs j JOIN order_items i ON i.id=j.order_item_id WHERE i.order_id=:id'), {'id':inserted[0]}).all()
    assert rows and all(status=='pending' and printer is None for _,status,printer in rows)
    assert not {row[0] for row in rows}.intersection(assignment.job_id for assignment in result.assignments)
