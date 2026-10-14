"""Actual durable dispatch must authorize preparation before attempt INSERT."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_dispatch_observation_binding_behavior import observed
from tests.test_contracts.test_education_review_workflow import review_db


@pytest.fixture(autouse=True)
def actual_order_parent_provider(monkeypatch):
    from core.registry import registry
    from modules.orders.production import OrderFulfillmentService
    monkeypatch.setitem(registry._providers,'OrderFulfillmentProvider',OrderFulfillmentService())


def test_exact_generic_dispatch_prepares_before_attempt_creation(observed):
    from core.attempt_start_plan import prepare_start
    from core.attempt_start_transaction import start_transaction
    engine, key = observed
    with engine.begin() as conn:
        filename = conn.execute(text('SELECT filename FROM print_jobs WHERE id=70')).scalar_one()
        conn.execute(text('DELETE FROM print_jobs WHERE id IN (70,71)'))
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            with start_transaction(conn) as scope:
                scope.fence_dispatch_membership(conn, education=True)
                plan = prepare_start(conn, scope, printer_id=59,
                                     observed_filename=filename, protocol='moonraker',
                                     observation_fields={'filename': filename, 'job_name': filename,
                                                         'dispatch_key': key})
                assert plan.branch == 'fresh_authorized'
                assert plan.job_id == 53
                assert conn.execute(text('SELECT count(*) FROM print_jobs')).scalar_one() == 0
                assert conn.execute(text('SELECT observed_print_job_id FROM dispatch_attempt_receipts WHERE job_id=53')).scalar_one() is None
        finally:
            transaction.rollback()


@pytest.mark.parametrize('mutation', [
    "UPDATE printer_dispatch_claims SET state='preparing' WHERE printer_id=59",
    "DELETE FROM dispatch_attempt_receipts",
    "UPDATE printers SET is_active=FALSE WHERE id=59",
])
def test_generic_prepare_never_authorizes_missing_or_changed_authority(observed, mutation):
    from core.attempt_start_plan import prepare_start
    from core.attempt_start_transaction import start_transaction
    engine, key = observed
    with engine.begin() as conn:
        filename = conn.execute(text('SELECT filename FROM print_jobs WHERE id=70')).scalar_one()
        conn.execute(text('DELETE FROM print_jobs WHERE id IN (70,71)'))
        conn.execute(text(mutation))
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            with start_transaction(conn) as scope:
                scope.fence_dispatch_membership(conn, education=True)
                plan = prepare_start(conn, scope, printer_id=59,
                    observed_filename=filename, protocol='moonraker',
                    observation_fields={'filename': filename, 'job_name': filename, 'dispatch_key': key})
                assert plan.branch == 'fresh_unresolved'
                assert plan.job_id is None
                assert conn.execute(text('SELECT count(*) FROM print_jobs')).scalar_one() == 0
        finally:
            transaction.rollback()


def _fresh(observed):
    engine,key=observed
    with engine.begin() as conn:
        name=conn.execute(text('SELECT filename FROM print_jobs WHERE id=70')).scalar_one()
        conn.execute(text('DELETE FROM print_jobs WHERE id IN (70,71)'))
    return engine,key,name


def _prepare(conn,scope,key,name):
    from core.attempt_start_plan import prepare_start
    scope.fence_dispatch_membership(conn,education=True)
    return prepare_start(conn,scope,printer_id=59,observed_filename=name,protocol='moonraker',observation_fields={'filename':name,'job_name':name,'dispatch_key':key})


def test_plan_owned_birth_finalizes_actual_receipt_and_resumes_without_rebirth(observed):
    from core.attempt_start_plan import create_start,finalize_start
    from core.attempt_start_transaction import start_transaction
    from core.economic_source_creation import SourceCreationError
    engine,key,name=_fresh(observed)
    with engine.begin() as conn:
        with start_transaction(conn) as scope:
            plan=_prepare(conn,scope,key,name)
            identifier=create_start(conn,scope,plan,{'started_at':'2026-10-13 00:00:00'})
            assert finalize_start(conn,scope,plan)==identifier
            with pytest.raises(SourceCreationError):finalize_start(conn,scope,plan)
        assert conn.execute(text('SELECT economic_birth_nonce,scheduled_job_id FROM print_jobs WHERE id=:id'),{'id':identifier}).one()[1]==53
        assert conn.execute(text('SELECT observed_print_job_id FROM dispatch_attempt_receipts WHERE job_id=53')).scalar_one()==identifier
    with engine.begin() as conn:
        with start_transaction(conn) as scope:
            plan=_prepare(conn,scope,key,name)
            assert plan.branch=='resume' and plan.resume_id==identifier
            assert finalize_start(conn,scope,plan)==identifier
        assert conn.execute(text('SELECT count(*) FROM cost_source_lifetimes')).scalar_one()==1


@pytest.mark.parametrize('mutation',["UPDATE printer_dispatch_claims SET state='preparing' WHERE printer_id=59",'DELETE FROM dispatch_attempt_receipts',"UPDATE printers SET api_host='changed.invalid' WHERE id=59"])
def test_prepared_authorized_drift_refuses_without_unknown_fallback(observed,mutation):
    from core.attempt_start_plan import create_start
    from core.attempt_start_transaction import start_transaction
    from core.economic_source_creation import SourceCreationError
    engine,key,name=_fresh(observed)
    with engine.connect() as conn:
        tx=conn.begin()
        with pytest.raises(SourceCreationError):
            with start_transaction(conn) as scope:
                plan=_prepare(conn,scope,key,name);assert plan.branch=='fresh_authorized'
                conn.execute(text(mutation));create_start(conn,scope,plan,{})
        tx.rollback()
    with engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM print_jobs')).scalar_one()==0
        assert conn.execute(text('SELECT count(*) FROM cost_source_lifetimes')).scalar_one()==0


def test_unknown_birth_and_outer_rollback_preserve_no_authority(observed):
    from core.attempt_start_plan import create_start,finalize_start
    from core.attempt_start_transaction import start_transaction
    engine,key,name=_fresh(observed)
    with engine.begin() as conn:conn.execute(text('DELETE FROM printer_dispatch_claims'))
    with engine.connect() as conn:
        tx=conn.begin()
        with start_transaction(conn) as scope:
            plan=_prepare(conn,scope,key,name);assert plan.branch=='fresh_unresolved'
            identifier=create_start(conn,scope,plan,{'started_at':'2026-10-13 00:00:00'})
            finalize_start(conn,scope,plan)
            assert conn.execute(text('SELECT scheduled_job_id,economic_uid FROM print_jobs WHERE id=:id'),{'id':identifier}).one()==(None,None)
        tx.rollback()
    with engine.connect() as conn:assert conn.execute(text('SELECT count(*) FROM cost_source_lifetimes')).scalar_one()==0


def test_legacy_resume_retains_null_birth_and_snapshot(observed):
    from core.dispatch_observations import bind_generic_observation
    from core.attempt_start_plan import finalize_start,create_start
    from core.attempt_start_transaction import start_transaction
    from core.economic_source_creation import SourceCreationError
    engine,key=observed
    assert bind_generic_observation(engine,printer_id=59,print_job_id=70,key=key)=='matched'
    with engine.begin() as conn:
        name=conn.execute(text('SELECT filename FROM print_jobs WHERE id=70')).scalar_one()
        with start_transaction(conn) as scope:
            plan=_prepare(conn,scope,key,name);assert plan.branch=='legacy_resume'
            with pytest.raises(SourceCreationError):create_start(conn,scope,plan,{})
            assert finalize_start(conn,scope,plan)==70
        assert conn.execute(text('SELECT economic_birth_nonce FROM print_jobs WHERE id=70')).scalar_one() is None


def test_substituted_plan_and_caller_identity_fields_refuse(observed):
    from copy import copy
    from core.attempt_start_plan import create_start
    from core.attempt_start_transaction import start_transaction
    from core.economic_source_creation import SourceCreationError
    engine,key,name=_fresh(observed)
    with engine.begin() as conn:
        with start_transaction(conn) as scope:
            plan=_prepare(conn,scope,key,name)
            with pytest.raises(SourceCreationError):create_start(conn,scope,copy(plan),{})
            with pytest.raises(SourceCreationError):create_start(conn,scope,plan,{'scheduled_job_id':54})


def test_finalize_refusal_rolls_back_owned_birth_and_linkage(observed, monkeypatch):
    from core import attempt_start_plan
    from core.attempt_start_transaction import start_transaction
    from core.economic_source_creation import SourceCreationError
    engine,key,name=_fresh(observed)
    with engine.connect() as conn:
        tx=conn.begin()
        with pytest.raises(SourceCreationError):
            with start_transaction(conn) as scope:
                plan=_prepare(conn,scope,key,name)
                attempt_start_plan.create_start(conn,scope,plan,{'started_at':'2026-10-13 00:00:00'})
                monkeypatch.setattr(attempt_start_plan,'_bind',lambda *a,**k:'refused')
                attempt_start_plan.finalize_start(conn,scope,plan)
        tx.rollback()
    with engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM cost_source_lifetimes')).scalar_one()==0
        assert conn.execute(text('SELECT count(*) FROM print_jobs')).scalar_one()==0
        assert conn.execute(text('SELECT observed_print_job_id FROM dispatch_attempt_receipts WHERE job_id=53')).scalar_one() is None


def test_plan_cannot_cross_executor_or_expired_scope(observed):
    from core.attempt_start_plan import create_start
    from core.attempt_start_transaction import start_transaction
    from core.economic_source_creation import SourceCreationError
    engine,key,name=_fresh(observed)
    with engine.connect() as conn:
        tx=conn.begin()
        with start_transaction(conn) as scope:
            plan=_prepare(conn,scope,key,name)
            with engine.connect() as foreign:
                foreign.begin()
                with pytest.raises(SourceCreationError):create_start(foreign,scope,plan,{})
                foreign.rollback()
        with pytest.raises(SourceCreationError):create_start(conn,scope,plan,{})
        tx.rollback()


def test_fresh_physical_timestamp_preserved_and_database_received_time_not_caller_supplied(observed):
    from core.attempt_start_plan import create_start,finalize_start
    from core.attempt_start_transaction import start_transaction
    from core.economic_source_creation import SourceCreationError
    engine,key,name=_fresh(observed)
    with engine.begin() as conn:
        with start_transaction(conn) as scope:
            plan=_prepare(conn,scope,key,name)
            with pytest.raises(SourceCreationError):create_start(conn,scope,plan,{'observed_received_at':'2001-01-01'})
            identifier=create_start(conn,scope,plan,{'started_at':'2001-01-01 00:00:00'})
            finalize_start(conn,scope,plan)
        row=conn.execute(text('SELECT started_at,observed_received_at FROM print_jobs WHERE id=:id'),{'id':identifier}).one()
        assert str(row[0]).startswith('2001-01-01') and not str(row[1]).startswith('2001')


def test_elegoo_physical_observation_is_unresolved_not_signed_dispatch_authority(observed):
    from core.attempt_start_plan import prepare_start,create_start,finalize_start
    from core.attempt_start_transaction import start_transaction
    engine,key,name=_fresh(observed)
    with engine.begin() as conn:conn.execute(text("UPDATE printers SET api_type='elegoo' WHERE id=59"))
    with engine.begin() as conn:
        with start_transaction(conn) as scope:
            scope.fence_dispatch_membership(conn,education=True)
            plan=prepare_start(conn,scope,printer_id=59,observed_filename=name,protocol='elegoo',observation_fields={'filename':name,'job_name':name,'dispatch_key':key})
            assert plan.branch=='fresh_unresolved' and plan.job_id is None
            identifier=create_start(conn,scope,plan,{'started_at':'2026-10-13 00:00:00'})
            assert finalize_start(conn,scope,plan)==identifier
        assert conn.execute(text('SELECT scheduled_job_id,economic_uid FROM print_jobs WHERE id=:id'),{'id':identifier}).one()==(None,None)
        assert conn.execute(text('SELECT observed_print_job_id FROM dispatch_attempt_receipts WHERE job_id=53')).scalar_one() is None


@pytest.mark.parametrize('legacy', [False,True])
def test_resume_requires_incoming_exact_retained_transport(observed,legacy):
    from core.attempt_start_plan import prepare_start,create_start,finalize_start
    from core.attempt_start_transaction import start_transaction
    from core.dispatch_observations import bind_generic_observation
    if legacy:
        engine,key=observed
        assert bind_generic_observation(engine,printer_id=59,print_job_id=70,key=key)=='matched'
    else:
        engine,key,name=_fresh(observed)
        with engine.begin() as conn:
            with start_transaction(conn) as scope:
                plan=_prepare(conn,scope,key,name)
                create_start(conn,scope,plan,{'started_at':'2026-10-13 00:00:00'});finalize_start(conn,scope,plan)
    with engine.begin() as conn:
        with start_transaction(conn) as scope:
            scope.fence_dispatch_membership(conn,education=True)
            wrong='different-observation.gcode'
            plan=prepare_start(conn,scope,printer_id=59,observed_filename=wrong,protocol='moonraker',observation_fields={'filename':wrong,'job_name':wrong,'dispatch_key':key})
            assert plan.branch=='fresh_unresolved' and plan.resume_id is None and plan.job_id is None


@pytest.mark.parametrize('legacy',[False,True])
def test_resume_finalization_reproves_exact_captured_physical_tuple(observed,legacy):
    from core.attempt_start_plan import finalize_start,create_start
    from core.attempt_start_transaction import start_transaction
    from core.dispatch_observations import bind_generic_observation
    from core.economic_source_creation import SourceCreationError
    if legacy:
        engine,key=observed;identifier=70
        assert bind_generic_observation(engine,printer_id=59,print_job_id=identifier,key=key)=='matched'
        with engine.connect() as conn:name=conn.execute(text('SELECT filename FROM print_jobs WHERE id=70')).scalar_one()
    else:
        engine,key,name=_fresh(observed)
        with engine.begin() as conn:
            with start_transaction(conn) as scope:
                plan=_prepare(conn,scope,key,name);identifier=create_start(conn,scope,plan,{'started_at':'2026-10-13 00:00:00'});finalize_start(conn,scope,plan)
    with engine.connect() as conn:
        tx=conn.begin()
        with pytest.raises(SourceCreationError):
            with start_transaction(conn) as scope:
                plan=_prepare(conn,scope,key,name)
                # Moonraker authority checker uses filename; unused job_name still
                # belongs to exact captured physical row and must not drift.
                conn.execute(text("UPDATE print_jobs SET job_name='changed-retained-name' WHERE id=:id"),{'id':identifier})
                finalize_start(conn,scope,plan)
        tx.rollback()
    with engine.connect() as conn:assert conn.execute(text('SELECT job_name FROM print_jobs WHERE id=:id'),{'id':identifier}).scalar_one()==name


@pytest.mark.parametrize('exception',[False,True])
def test_scope_exit_clears_key_and_poisoned_records_even_when_scope_retained(observed,exception):
    from core.attempt_start_plan import create_start
    from core.attempt_start_transaction import start_transaction
    from core.economic_source_creation import SourceCreationError
    engine,key,name=_fresh(observed);scope=plan=record=None
    with engine.connect() as conn:
        tx=conn.begin()
        try:
            with start_transaction(conn) as scope:
                plan=_prepare(conn,scope,key,name)
                record=scope._start_plan_records[id(plan)]
                assert record['key'] is key
                if exception:raise RuntimeError('owned scope exit')
        except RuntimeError:
            assert exception
        assert scope._start_plan_records=={} and record=={'finalized':True}
        with pytest.raises(SourceCreationError):create_start(conn,scope,plan,{})
        tx.rollback()


def test_private_capture_context_requires_owned_fresh_once_and_completed_cas(observed):
    from core.attempt_start_plan import prepare_start,create_start,finalize_start,_fresh_capture_context,_complete_fresh_capture
    from core.attempt_start_transaction import start_transaction
    from core.economic_source_creation import SourceCreationError
    engine,key,name=_fresh(observed)
    with engine.begin() as c:
        with start_transaction(c) as scope:
            scope.fence_dispatch_membership(c,education=True)
            plan=prepare_start(c,scope,printer_id=59,observed_filename=name,protocol='moonraker',observation_fields={'filename':name,'job_name':name,'dispatch_key':key},capture_required=True)
            identifier=create_start(c,scope,plan,{'started_at':'2026-10-13 00:00:00'})
            context=_fresh_capture_context(c,scope,plan,identifier)
            assert context.id==identifier and context.birth_nonce and context.identity_uid is None and context.job_id==53
            assert not hasattr(context,'dispatch_key')
            with pytest.raises(SourceCreationError):_fresh_capture_context(c,scope,plan,identifier+1)
            with pytest.raises(SourceCreationError):_complete_fresh_capture(c,scope,plan,identifier)
            with pytest.raises(SourceCreationError):finalize_start(c,scope,plan)
            # Scope protocol control only; root owns real valuation snapshot tests.
            c.execute(text('UPDATE print_jobs SET spool_snapshot=:snapshot WHERE id=:id AND spool_snapshot IS NULL'),{'snapshot':'{"version":1,"printer_id":59,"material_plan":[]}','id':identifier})
            _complete_fresh_capture(c,scope,plan,identifier)
            with pytest.raises(SourceCreationError):_fresh_capture_context(c,scope,plan,identifier)
            with pytest.raises(SourceCreationError):_complete_fresh_capture(c,scope,plan,identifier)
            assert finalize_start(c,scope,plan)==identifier
