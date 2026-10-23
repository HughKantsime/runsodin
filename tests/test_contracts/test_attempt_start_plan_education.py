"""Actual canonical EDU dispatch authority before plan-owned physical INSERT."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_education_dispatch_receipt_binding import admitted_education
from tests.test_contracts.test_education_dispatch_policy import actual_education_dispatch
from tests.test_contracts.test_education_review_workflow import review_db

@pytest.fixture(autouse=True)
def actual_parents(monkeypatch):
    from core.registry import registry
    from modules.orders.production import OrderFulfillmentService
    monkeypatch.setitem(registry._providers,'OrderFulfillmentProvider',OrderFulfillmentService())


def prepare(c,scope,name):
    from core.attempt_start_plan import prepare_start
    from core import auth
    scope.fence_dispatch_membership(c,education=True)
    return prepare_start(c,scope,printer_id=9,observed_filename=name,protocol='bambu',observation_fields={'filename':'Metadata/plate_1.gcode','job_name':name,'dispatch_key':auth.SECRET_KEY.encode()})


@pytest.mark.parametrize('admitted_education',['confirmed','reserved'],indirect=True)
def test_actual_education_preinsert_then_owned_birth_transition_and_resume(admitted_education):
    from core.attempt_start_plan import create_start,finalize_start
    from core.attempt_start_transaction import start_transaction
    engine,uploaded,name,_=admitted_education
    with engine.begin() as c:
        revision=c.execute(text('SELECT lifecycle_revision FROM education_submissions WHERE id=:id'),{'id':uploaded['id']}).scalar_one()
        with start_transaction(c) as scope:
            plan=prepare(c,scope,name);assert plan.branch=='fresh_authorized' and plan.job_id==uploaded['job_id']
            assert c.execute(text('SELECT count(*) FROM print_jobs')).scalar_one()==0
            identifier=create_start(c,scope,plan,{'started_at':'2026-10-13 00:00:00'})
            assert finalize_start(c,scope,plan)==identifier
        assert c.execute(text('SELECT scheduled_job_id,economic_uid FROM print_jobs WHERE id=:id'),{'id':identifier}).one()==(uploaded['job_id'],None)
        assert c.execute(text('SELECT status FROM education_submissions WHERE id=:id'),{'id':uploaded['id']}).scalar_one()=='printing'
        assert c.execute(text('SELECT state FROM education_monitor_claims WHERE job_id=:id'),{'id':uploaded['job_id']}).scalar_one()=='running'
        assert c.execute(text('SELECT observed_print_job_id FROM dispatch_attempt_receipts WHERE job_id=:id'),{'id':uploaded['job_id']}).scalar_one()==identifier
    with engine.begin() as c:
        with start_transaction(c) as scope:
            plan=prepare(c,scope,name);assert plan.branch=='resume';assert finalize_start(c,scope,plan)==identifier
        assert c.execute(text('SELECT count(*) FROM cost_source_lifetimes')).scalar_one()==1


@pytest.mark.parametrize('mutation',["UPDATE education_cost_centers SET state='archived'", "UPDATE education_cost_center_printers SET state='revoked'", "UPDATE education_monitor_claims SET authority_revision=authority_revision+1"])
def test_prepared_education_drift_outer_rolls_back_no_birth(admitted_education,mutation):
    from core.attempt_start_plan import create_start
    from core.attempt_start_transaction import start_transaction
    from core.economic_source_creation import SourceCreationError
    engine,uploaded,name,_=admitted_education
    with engine.connect() as c:
        tx=c.begin()
        with pytest.raises(SourceCreationError):
            with start_transaction(c) as scope:
                plan=prepare(c,scope,name);assert plan.branch=='fresh_authorized'
                c.execute(text(mutation));create_start(c,scope,plan,{'started_at':'2026-10-13 00:00:00'})
        tx.rollback()
    with engine.connect() as c:assert c.execute(text('SELECT count(*) FROM print_jobs')).scalar_one()==0


def test_actual_legacy_education_resume_does_not_birth_or_promote(admitted_education):
    from core.attempt_start_plan import finalize_start,_fresh_capture_context
    from core.attempt_start_transaction import start_transaction
    from core.economic_source_creation import SourceCreationError
    engine,uploaded,name,start=admitted_education
    from modules.organizations.education_policy import claim_monitor_observation
    import time
    # SQLite CURRENT_TIMESTAMP is whole-second precision; observe strictly
    # after the genuine admission timestamp instead of fabricating its clock.
    time.sleep(1.1)
    # Explicit retained pre-foundation physical history; never run fresh creation.
    with engine.begin() as c:
        identifier=c.execute(text("INSERT INTO print_jobs(printer_id,filename,job_name,started_at,status,spool_snapshot,observed_received_at) VALUES(9,:name,:name,CURRENT_TIMESTAMP,'running',:snapshot,CURRENT_TIMESTAMP) RETURNING id"), {"name":name,"snapshot":"{\"legacy\":true}"}).scalar_one()
        result=claim_monitor_observation(c,print_job_id=identifier,printer_id=9,observed_filename=name)
        assert result['authorized'] and result['dispatch_receipt_binding']=='matched'

    with engine.begin() as c:
        old=c.execute(text('SELECT economic_birth_nonce,spool_snapshot FROM print_jobs WHERE id=:id'),{'id':identifier}).one()
        assert old[0] is None
        with start_transaction(c) as scope:
            plan=prepare(c,scope,name);assert plan.branch=='legacy_resume'
            with pytest.raises(SourceCreationError):_fresh_capture_context(c,scope,plan,identifier)
            assert finalize_start(c,scope,plan)==identifier
        assert c.execute(text('SELECT economic_birth_nonce,spool_snapshot FROM print_jobs WHERE id=:id'),{'id':identifier}).one()==old


def test_without_prepared_capability_existing_prelinked_row_does_not_widen_cas(admitted_education):
    from core.attempt_start_plan import create_start
    from core.attempt_start_transaction import start_transaction
    from modules.organizations.education_policy import claim_monitor_observation
    engine,uploaded,name,_=admitted_education
    with engine.connect() as c:
        tx=c.begin()
        with start_transaction(c) as scope:
            plan=prepare(c,scope,name)
            identifier=create_start(c,scope,plan,{'started_at':'2026-10-13 00:00:00'})
            result=claim_monitor_observation(c,print_job_id=identifier,printer_id=9,observed_filename=name)
            assert not result.get('authorized')
        tx.rollback()
    with engine.connect() as c:assert c.execute(text('SELECT count(*) FROM print_jobs')).scalar_one()==0


def test_education_audit_failure_requires_whole_outer_rollback(admitted_education):
    from core.attempt_start_plan import create_start,finalize_start
    from core.attempt_start_transaction import start_transaction
    engine,uploaded,name,_=admitted_education
    with engine.begin() as c:
        before=tuple(c.execute(text('SELECT status,lifecycle_revision FROM education_submissions WHERE id=:id'),{'id':uploaded['id']}).one())
        c.exec_driver_sql("CREATE TRIGGER owned_start_audit_fault BEFORE INSERT ON education_audit_events BEGIN SELECT RAISE(ABORT,'owned audit failure'); END")
    with engine.connect() as c:
        tx=c.begin()
        with pytest.raises(Exception,match='owned audit failure'):
            with start_transaction(c) as scope:
                plan=prepare(c,scope,name);create_start(c,scope,plan,{'started_at':'2026-10-13 00:00:00'});finalize_start(c,scope,plan)
        tx.rollback()
    with engine.begin() as c:
        assert tuple(c.execute(text('SELECT status,lifecycle_revision FROM education_submissions WHERE id=:id'),{'id':uploaded['id']}).one())==before
        assert c.execute(text('SELECT count(*) FROM print_jobs')).scalar_one()==0
        assert c.execute(text('SELECT count(*) FROM cost_source_lifetimes')).scalar_one()==0
        c.exec_driver_sql('DROP TRIGGER owned_start_audit_fault')


@pytest.mark.parametrize('legacy',[False,True])
@pytest.mark.parametrize('conflict_side',['incoming','retained'])
def test_education_resume_rejects_contradictory_incoming_or_retained_tokens(admitted_education,legacy,conflict_side):
    from core.attempt_start_plan import prepare_start,create_start,finalize_start
    from core.attempt_start_transaction import start_transaction
    from core import auth
    engine,uploaded,name,start=admitted_education
    if legacy:
        identifier=start()
    else:
        with engine.begin() as c:
            with start_transaction(c) as scope:
                plan=prepare(c,scope,name);identifier=create_start(c,scope,plan,{'started_at':'2026-10-13 00:00:00'});finalize_start(c,scope,plan)
    contradictory='odin-'+('f'*32 if 'f'*32 not in name else 'e'*32)+'.3mf'
    with engine.connect() as c:
        tx=c.begin()
        if conflict_side=='retained':c.execute(text('UPDATE print_jobs SET filename=:name WHERE id=:id'),{'name':contradictory,'id':identifier})
        with start_transaction(c) as scope:
            scope.fence_dispatch_membership(c,education=True)
            plan=prepare_start(c,scope,printer_id=9,observed_filename=name,protocol='bambu',observation_fields={'filename':contradictory if conflict_side=='incoming' else 'Metadata/plate_1.gcode','job_name':name,'dispatch_key':auth.SECRET_KEY.encode()})
            assert plan.branch=='fresh_unresolved' and plan.resume_id is None and plan.job_id is None
        tx.rollback()


@pytest.mark.parametrize('legacy',[False,True])
def test_education_final_resume_rechecks_canonical_identity_conflicts(admitted_education,legacy):
    from core.attempt_start_plan import create_start,finalize_start
    from core.attempt_start_transaction import start_transaction
    from core.economic_source_creation import SourceCreationError
    engine,uploaded,name,start=admitted_education
    if legacy:identifier=start()
    else:
        with engine.begin() as c:
            with start_transaction(c) as scope:
                plan=prepare(c,scope,name);identifier=create_start(c,scope,plan,{'started_at':'2026-10-13 00:00:00'});finalize_start(c,scope,plan)
    with engine.connect() as c:
        tx=c.begin()
        with pytest.raises(SourceCreationError):
            with start_transaction(c) as scope:
                plan=prepare(c,scope,name);assert plan.branch in ('resume','legacy_resume')
                c.execute(text('UPDATE print_jobs SET filename=:name WHERE id=:id'),{'name':'odin-'+('d'*32)+'.3mf','id':identifier})
                finalize_start(c,scope,plan)
        tx.rollback()


def test_canonical_preinsert_status_predicate_stays_closed_with_mixed_storage_types(admitted_education):
    from modules.organizations.education_policy import discover_start_authority,recheck_start_authority
    engine,uploaded,name,_=admitted_education
    with engine.connect() as c:
        tx=c.begin()
        context=discover_start_authority(c,printer_id=9,observed_filename=name)
        assert recheck_start_authority(c,context)
        # Submission remains printing, but scheduled/completed job is not the
        # same awaiting/running authority. PG uses enum, submission uses text.
        for status in ('scheduled','completed'):
            c.execute(text('UPDATE jobs SET status=:status WHERE id=:id'),{'status':status,'id':uploaded['job_id']})
            assert not recheck_start_authority(c,context)
        tx.rollback()


def test_anycubic_never_promotes_retained_education_authority(admitted_education):
    from core.attempt_start_plan import prepare_start
    from core.attempt_start_transaction import start_transaction
    from core import auth
    engine, uploaded, name, _ = admitted_education
    with engine.begin() as conn:
        conn.execute(text("UPDATE printers SET api_type='anycubic_lan' WHERE id=9"))
        before = conn.execute(text('SELECT status FROM education_submissions WHERE id=:id'), {'id':uploaded['id']}).scalar_one()
        with start_transaction(conn) as scope:
            scope.fence_dispatch_membership(conn, education=True)
            plan = prepare_start(conn, scope, printer_id=9, protocol='anycubic_lan',
                observed_filename=name, observation_fields={'filename':name,'job_name':name,'dispatch_key':auth.SECRET_KEY.encode()})
            assert plan.branch == 'fresh_unresolved' and plan.job_id is None and plan.reserved
        assert conn.execute(text('SELECT status FROM education_submissions WHERE id=:id'), {'id':uploaded['id']}).scalar_one() == before
