"""Slice 1–3: frozen terminal valuation, transactional replay and private aggregates."""
from decimal import Decimal
import json
import pytest
from sqlalchemy import text
from tests.test_contracts.test_private_attempt_valuation_route import private_attempt, creators, review_db
from modules.archives.measured_filament import calculate, measurement, retain_terminal, revise_valuation


@pytest.mark.parametrize('grams,price,net,expected',[(Decimal('12.5'),'20','1000','0.25'),(Decimal('0'),'20','1000','0'),(Decimal('3.125'),'0','750','0.000'),(Decimal('1'),'1','3',None)])
def test_decimal_calculation(grams,price,net,expected):
    result=calculate(grams,price,net)
    if expected is not None:assert Decimal(result)==Decimal(expected)
    else:assert result == '0.333333'


@pytest.mark.parametrize('value',[True,-1,float('nan'),float('inf'),1e20,Decimal('0.0000000000001'),Decimal('0E+1000000000'),'1'])
def test_invalid_measurements(value):assert measurement(value)==('invalid',None)


@pytest.fixture
def terminal(private_attempt,review_db):
    client,auth,token,identifier=private_attempt
    review_db.execute(text("UPDATE print_jobs SET status='completed',filament_used_g=12.5 WHERE id=:id"),{'id':identifier})
    observation=retain_terminal(review_db.connection(),identifier)
    review_db.commit()
    order=review_db.execute(text('SELECT i.order_id FROM print_jobs p JOIN jobs j ON j.id=p.scheduled_job_id JOIN order_items i ON i.id=j.order_item_id WHERE p.id=:id'),{'id':identifier}).scalar_one()
    review_db.rollback()
    return client,auth,token,identifier,observation,order


def test_terminal_exact_frozen_replay_and_private_aggregate(terminal,review_db):
    client,auth,_,identifier,observation,order=terminal
    original=review_db.execute(text('SELECT amount_decimal FROM filament_cost_events WHERE observation_id=:id'),{'id':observation}).scalar_one()
    assert Decimal(original)==Decimal(calculate(Decimal('12.5'),'12.99','750.25'))
    review_db.execute(text('UPDATE spools SET price=999,initial_weight_g=1'))
    assert retain_terminal(review_db.connection(),identifier)==observation
    assert review_db.execute(text('SELECT count(*) FROM filament_cost_events')).scalar_one()==1
    review_db.commit()
    response=client.get(f'/orders/{order}/measured-costs',headers=auth('platform'))
    assert response.status_code==200,response.text
    value=response.json()
    assert response.headers['cache-control']=='private, no-store'
    assert value['measured_filament_known_subtotal']=='0.22'
    assert value['filament_state']=='partial'
    assert value['production_cogs_state']=='unavailable'
    assert value['known_attempts']==1
    assert 'price_decimal' not in response.text and 'spool_uid' not in response.text


def test_conflicting_callback_does_not_overwrite(terminal,review_db):
    _,_,_,identifier,observation,_=terminal
    review_db.execute(text('UPDATE print_jobs SET filament_used_g=13 WHERE id=:id'),{'id':identifier})
    with pytest.raises(ValueError,match='Conflicting'):retain_terminal(review_db.connection(),identifier)
    review_db.rollback()
    assert review_db.execute(text('SELECT measured_grams FROM terminal_filament_measurements WHERE observation_id=:id'),{'id':observation}).scalar_one()=='12.5'


def test_caller_rollback_and_uncertain_commit_replay(private_attempt,review_db):
    _,_,_,identifier=private_attempt
    review_db.execute(text("UPDATE print_jobs SET status='failed',filament_used_g=5 WHERE id=:id"),{'id':identifier})
    retain_terminal(review_db.connection(),identifier)
    review_db.rollback()
    assert review_db.execute(text('SELECT count(*) FROM terminal_filament_measurements')).scalar_one()==0
    review_db.execute(text("UPDATE print_jobs SET status='failed',filament_used_g=5 WHERE id=:id"),{'id':identifier})
    original=retain_terminal(review_db.connection(),identifier);review_db.commit()
    assert retain_terminal(review_db.connection(),identifier)==original
    assert review_db.execute(text('SELECT count(*) FROM filament_cost_events')).scalar_one()==1


@pytest.mark.parametrize('name',['admin','foreign','unassigned'])
def test_foreign_order_refused_without_amount(terminal,name):
    client,auth,_,_,_,order=terminal
    response=client.get(f'/orders/{order}/measured-costs',headers=auth(name))
    assert response.status_code==404,response.text
    assert response.headers['cache-control']=='private, no-store'
    assert '0.22' not in response.text


@pytest.mark.parametrize('scopes',[['read:jobs'],['read:spools'],['write']])
def test_narrow_tokens_refused(terminal,scopes):
    client,_,token,_,_,order=terminal
    response=client.get(f'/orders/{order}/measured-costs',headers=token(scopes))
    assert response.status_code==403,response.text


def test_hidden_content_refuses_entire_total(terminal,review_db):
    client,auth,_,_,_,order=terminal
    review_db.execute(text('UPDATE print_files SET stored_path=(SELECT stored_path FROM print_files WHERE id=58) WHERE id=52'));review_db.commit()
    response=client.get(f'/orders/{order}/measured-costs',headers=auth('platform'))
    assert response.status_code==404,response.text


def test_snapshot_edit_does_not_revalue_retained_total(terminal,review_db):
    client,auth,_,identifier,_,order=terminal
    review_db.execute(text("UPDATE print_jobs SET spool_snapshot='{}' WHERE id=:id"),{'id':identifier});review_db.commit()
    response=client.get(f'/orders/{order}/measured-costs',headers=auth('platform'))
    assert response.status_code==200,response.text
    assert response.json()['measured_filament_known_subtotal']=='0.22'


@pytest.mark.parametrize('outcome,grams,reason',[('failed',None,'missing_measurement'),('cancelled',None,'missing_measurement'),('completed',0,None)])
def test_failure_missing_and_zero(private_attempt,review_db,outcome,grams,reason):
    client,auth,_,identifier=private_attempt
    review_db.execute(text('UPDATE print_jobs SET status=:status,filament_used_g=:grams WHERE id=:id'),{'status':outcome,'grams':grams,'id':identifier})
    observation=retain_terminal(review_db.connection(),identifier);review_db.commit()
    event=review_db.execute(text('SELECT reason,amount_decimal FROM filament_cost_events WHERE observation_id=:id'),{'id':observation}).first()
    assert event[0]==reason
    assert (event[1] is None) == (reason is not None)
    if grams==0:assert Decimal(event[1])==0


def test_open_attempt_is_incomplete(private_attempt,review_db):
    client,auth,_,identifier=private_attempt
    order=review_db.execute(text('SELECT i.order_id FROM print_jobs p JOIN jobs j ON j.id=p.scheduled_job_id JOIN order_items i ON i.id=j.order_item_id WHERE p.id=:id'),{'id':identifier}).scalar_one();review_db.rollback()
    response=client.get(f'/orders/{order}/measured-costs',headers=auth('platform'))
    assert response.status_code==200,response.text
    assert response.json()['open_attempts']==1
    assert response.json()['measured_filament_known_subtotal'] is None


def test_subtotals_retry_reject_and_currency_contract():
    from modules.archives.order_measured_costs import summarize
    def attempt(id,outcome,amount,reason=None,currency='USD'):
        return dict(attempt_id=id,outcome=outcome,measurement_basis='printer_reported',amount=amount,reason=reason,currency=currency if amount is not None else None)
    # A rejected completed plate still consumes filament; the replacement is separate.
    values=[attempt(1,'completed','2'),attempt(2,'failed','1.1'),attempt(3,'completed','0.25')]
    value=summarize(values)
    assert value['measured_filament_known_subtotal']=='3.35'
    assert value['successful_subtotal']=='2.25' and value['failed_cancelled_subtotal']=='1.10'
    assert summarize(values+[attempt(4,'failed',None,'missing_measurement')])['filament_state']=='partial'
    mixed=summarize(values+[attempt(4,'completed','1',currency='EUR')])
    assert mixed['measured_filament_known_subtotal'] is None and mixed['currency'] is None
    assert 'currency_mismatch' in mixed['reasons']
    assert summarize([])['measured_filament_known_subtotal'] is None


def test_append_only_correction_expected_revision_and_no_double_count(terminal,review_db):
    client,auth,_,_,observation,order=terminal
    from sqlalchemy.exc import IntegrityError
    revise_valuation(review_db.connection(),observation,expected_revision=1,audit_reason='Rechecked original purchase evidence')
    review_db.commit()
    assert review_db.execute(text('SELECT count(*) FROM filament_cost_events WHERE observation_id=:id'),{'id':observation}).scalar_one()==2
    with pytest.raises(IntegrityError):revise_valuation(review_db.connection(),observation,expected_revision=1,audit_reason='Stale revision')
    review_db.rollback()
    result=client.get(f'/orders/{order}/measured-costs',headers=auth('platform'))
    assert result.status_code==200,result.text
    assert result.json()['measured_filament_known_subtotal']=='0.22'
    assert result.json()['known_attempts']==1


@pytest.mark.parametrize('statement',[
 'UPDATE terminal_filament_measurements SET measured_grams=\'99\'',
 'DELETE FROM terminal_filament_measurements',
 'UPDATE filament_cost_events SET amount_decimal=\'99\'',
 'DELETE FROM filament_cost_events',
 'INSERT OR REPLACE INTO filament_cost_events SELECT * FROM filament_cost_events',
 'INSERT OR REPLACE INTO terminal_filament_measurements SELECT * FROM terminal_filament_measurements',
])
def test_retained_evidence_immutable_even_replace(terminal,review_db,statement):
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):review_db.execute(text(statement))
    review_db.rollback()


def test_deletion_numeric_id_reuse_never_reattaches(terminal,review_db):
    client,auth,_,identifier,observation,order=terminal
    review_db.execute(text('DELETE FROM print_jobs WHERE id=:id'),{'id':identifier})
    review_db.execute(text("INSERT INTO print_jobs(id,printer_id,job_name,status,started_at) VALUES(:id,9,'Reused numeric id','completed',CURRENT_TIMESTAMP)"),{'id':identifier})
    review_db.commit()
    assert review_db.execute(text('SELECT count(*) FROM terminal_filament_measurements WHERE observation_id=:id'),{'id':observation}).scalar_one()==1
    review_db.rollback()
    result=client.get(f'/orders/{order}/measured-costs',headers=auth('platform'))
    assert result.status_code==404,result.text


@pytest.mark.parametrize('change,reason',[
 ('legacy','missing_historical_valuation'),('multi','unresolved_allocation'),('currency','currency_mismatch'),('receipt','unresolved_purchase_evidence')])
def test_unavailable_inputs_retained(private_attempt,review_db,change,reason):
    _,_,_,identifier=private_attempt
    snapshot=json.loads(review_db.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=:id'),{'id':identifier}).scalar_one())
    if change=='legacy':snapshot.pop('unit_valuation')
    elif change=='multi':snapshot['material_plan']['allocations']*=2
    elif change=='currency':
        snapshot['unit_valuation']['target']['currency']='EUR'
        snapshot['unit_valuation'].update(state='unknown',reason='unresolved_source_valuation')
        snapshot['unit_valuation']['sources'][0].update(state='unknown',reason='currency_mismatch')
    elif change=='receipt':
        source=snapshot['unit_valuation']['sources'][0]
        for key in list(source):
            if key not in ('spool_uid','birth_nonce','state','reason'):source.pop(key)
        source.update(state='unknown',reason='missing_purchase_evidence')
        snapshot['unit_valuation'].update(state='unknown',reason='unresolved_source_valuation')
    review_db.execute(text("UPDATE print_jobs SET status='failed',filament_used_g=10,spool_snapshot=:snapshot WHERE id=:id"),{'id':identifier,'snapshot':json.dumps(snapshot)})
    observation=retain_terminal(review_db.connection(),identifier);review_db.commit()
    assert review_db.execute(text('SELECT reason FROM filament_cost_events WHERE observation_id=:id'),{'id':observation}).scalar_one()==reason


@pytest.fixture
def archive_terminal(private_attempt,review_db,monkeypatch):
    from contextlib import contextmanager
    from modules.archives import archive
    client,auth,token,identifier=private_attempt
    review_db.execute(text("UPDATE print_jobs SET status='completed',filament_used_g=12.5 WHERE id=:id"),{'id':identifier})
    printer,job=review_db.execute(text('SELECT printer_id,scheduled_job_id FROM print_jobs WHERE id=:id'),{'id':identifier}).one()
    review_db.commit()
    engine=review_db.get_bind()
    @contextmanager
    def database():
        connection=engine.raw_connection()
        try:yield connection
        finally:connection.close()
    monkeypatch.setattr(archive,'get_db',database)
    return archive,engine,identifier,printer,job


def test_archive_writes_cost_even_after_earlier_monitor_deduction(archive_terminal):
    from core.filament_ledger import print_marker
    archive,engine,identifier,printer,job=archive_terminal
    with engine.begin() as c:
        c.execute(text('INSERT INTO system_config(key,value) VALUES(:key,:value)'),{'key':print_marker(job,identifier),'value':'earlier-monitor'})
    archive_id=archive.create_print_archive(identifier,printer,True)
    assert archive_id is not None
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM terminal_filament_measurements')).scalar_one()==1
        assert c.execute(text('SELECT count(*) FROM filament_cost_events WHERE amount_decimal IS NOT NULL')).scalar_one()==1
        assert c.execute(text('SELECT accounting_reason FROM print_archives WHERE id=:id'),{'id':archive_id}).scalar_one()=='attempt_already_accounted'
    assert archive.create_print_archive(identifier,printer,True)==archive_id


def test_event_failure_preserves_archive_inventory_and_outbox(archive_terminal,monkeypatch):
    from modules.archives import measured_filament
    archive,engine,identifier,printer,_=archive_terminal
    with engine.begin() as c:
        c.execute(text('UPDATE spools SET spoolman_spool_id=123'))
        before=c.execute(text('SELECT id,remaining_weight_g FROM spools ORDER BY id')).all()
    def refuse(*args,**kwargs):raise RuntimeError('injected event persistence failure')
    monkeypatch.setattr(measured_filament,'_append_event',refuse)
    assert archive.create_print_archive(identifier,printer,True) is not None
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM print_archives WHERE print_job_id=:id'),{'id':identifier}).scalar_one()==1
        # Inventory accounting is untouched by a cost-capture failure.
        assert c.execute(text("SELECT accounting_state FROM print_archives WHERE print_job_id=:id"),{'id':identifier}).scalar_one()!='unresolved'
        assert c.execute(text('SELECT count(*) FROM terminal_filament_measurements')).scalar_one()==0
        assert c.execute(text('SELECT count(*) FROM filament_delivery_outbox')).scalar_one()==1
        assert c.execute(text('SELECT id,remaining_weight_g FROM spools ORDER BY id')).all()!=before


def test_concurrent_archive_delivery_is_one_physical_event(archive_terminal):
    from concurrent.futures import ThreadPoolExecutor
    archive,engine,identifier,printer,_=archive_terminal
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:archive.create_print_archive(identifier,printer,True),range(2)))
    assert results[0] is not None and results[0]==results[1]
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM terminal_filament_measurements')).scalar_one()==1
        assert c.execute(text('SELECT count(*) FROM filament_cost_events')).scalar_one()==1


from tests.test_contracts.test_private_attempt_valuation_route import org_attempt, production_order, bom_client


def test_org_owner_and_wrong_tenant_order_costs(org_attempt,review_db):
    client,auth,_,identifier=org_attempt
    review_db.execute(text("UPDATE print_jobs SET status='completed',filament_used_g=0 WHERE id=:id"),{'id':identifier})
    retain_terminal(review_db.connection(),identifier)
    order=review_db.execute(text('SELECT i.order_id FROM print_jobs p JOIN jobs j ON j.id=p.scheduled_job_id JOIN order_items i ON i.id=j.order_item_id WHERE p.id=:id'),{'id':identifier}).scalar_one()
    review_db.commit()
    result=client.get(f'/orders/{order}/measured-costs',headers=auth())
    assert result.status_code==200,result.text
    assert result.json()['measured_filament_known_subtotal']=='0.00'
    assert result.json()['filament_state']=='partial'
    for actor in ('foreign','unassigned'):
        denied=client.get(f'/orders/{order}/measured-costs',headers=auth(actor))
        assert denied.status_code==404,denied.text


def test_inventory_shortfall_remains_visible_without_clamping(archive_terminal):
    archive,engine,identifier,printer,_=archive_terminal
    with engine.begin() as c:c.execute(text('UPDATE spools SET remaining_weight_g=0'))
    assert archive.create_print_archive(identifier,printer,True) is not None
    with engine.connect() as c:
        row=c.execute(text('SELECT measured_grams,reconciliation_reason FROM terminal_filament_measurements')).one()
        assert tuple(row)==('12.5','inventory_shortfall')
        assert c.execute(text('SELECT amount_decimal FROM filament_cost_events')).scalar_one()==calculate(Decimal('12.5'),'12.99','750.25')


def test_existing_archive_is_not_historically_backfilled(archive_terminal):
    archive,engine,identifier,printer,_=archive_terminal
    with engine.begin() as c:
        c.execute(text("INSERT INTO print_archives(print_job_id,printer_id,print_name,status) VALUES(:id,:printer,'Historical archive','completed')"),{'id':identifier,'printer':printer})
    assert archive.create_print_archive(identifier,printer,True) is not None
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM terminal_filament_measurements')).scalar_one()==0
        assert c.execute(text('SELECT count(*) FROM filament_cost_events')).scalar_one()==0


def test_sum_unrounded_costs_then_round_once():
    from modules.archives.order_measured_costs import summarize
    attempts=[dict(attempt_id=i,outcome='completed',measurement_basis='printer_reported',amount='0.005',currency='USD',reason=None) for i in (1,2)]
    assert summarize(attempts)['measured_filament_known_subtotal']=='0.01'
    with pytest.raises(ValueError):calculate(Decimal('1000000000'),'999999999999999999','0.000000000001')


def test_same_owner_wrong_order_observation_refused(private_attempt,review_db):
    from sqlalchemy.exc import IntegrityError
    from uuid import uuid4
    _,_,_,identifier=private_attempt
    row=review_db.execute(text('SELECT economic_uid,economic_birth_nonce FROM print_jobs WHERE id=:id'),{'id':identifier}).one()
    binding=review_db.execute(text('SELECT item_uid FROM cost_attempt_bindings WHERE attempt_uid=:uid'),{'uid':row[0]}).one()
    other=str(uuid4())
    review_db.execute(text("INSERT INTO cost_identities(identity_uid,resource_kind,resource_id,owner_scope,owner_key) VALUES(:uid,'order',99999,'global',0)"),{'uid':other})
    with pytest.raises(IntegrityError):
        review_db.execute(text("""INSERT INTO terminal_filament_measurements
          (observation_id,attempt_uid,attempt_birth,owner_scope,owner_key,order_uid,item_uid,allocation_key,outcome,measurement_basis,snapshot_json)
          VALUES(:observation,:attempt,:birth,'global',0,:wrong_order,:item,'attempt-total','failed','unavailable','{}')"""),
          {'observation':uuid4().hex,'attempt':row[0],'birth':row[1],'wrong_order':other,'item':binding[0]})
    review_db.rollback()


def test_mixed_order_consumption_has_no_amount(private_attempt,review_db):
    # Fault-inject a retained participation mismatch at the transaction-view
    # boundary. The normal foundation rejects such cross-order inserts itself.
    from core.dispatch_observation_executor import TransactionView
    from modules.archives.valuation_financial_graph import prove_financial_graph
    from modules.archives.measured_filament import value_observation
    from types import SimpleNamespace
    _,_,_,identifier=private_attempt
    graph=prove_financial_graph(review_db,identifier)
    snapshot=review_db.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=:id'),{'id':identifier}).scalar_one()
    class MixedParticipation:
        def execute(self,statement,parameters=None):
            if 'FROM cost_attempt_participation' in str(statement):return SimpleNamespace(first=lambda:(1,))
            return review_db.execute(statement,parameters or {})
    value=value_observation(MixedParticipation(),graph,snapshot,'printer_reported','12.5')
    assert value['amount_decimal'] is None and value['reason']=='mixed_order_plate'


def test_adoption_rejects_tainted_existing_same_owner_order(terminal,review_db):
    import importlib
    from uuid import uuid4
    from core.schema.migrator import MigrationError
    migration=importlib.import_module('core.schema.migrations.039_measured_filament')
    other=str(uuid4())
    review_db.execute(text("INSERT INTO cost_identities(identity_uid,resource_kind,resource_id,owner_scope,owner_key) VALUES(:uid,'order',99999,'global',0)"),{'uid':other})
    # Simulate a damaged database whose historical guard was subsequently repaired.
    review_db.execute(text('DROP TRIGGER terminal_filament_measurements_no_update'))
    review_db.execute(text('UPDATE terminal_filament_measurements SET order_uid=:uid'),{'uid':other})
    review_db.execute(text(migration.sqlite_triggers()['terminal_filament_measurements_no_update']))
    with pytest.raises(MigrationError,match='retained association'):migration.validate(review_db.connection())
    review_db.rollback()


def test_legacy_attempt_is_unresolved_without_identifier(terminal,review_db):
    client,auth,_,identifier,_,order=terminal
    job=review_db.execute(text('SELECT scheduled_job_id FROM print_jobs WHERE id=:id'),{'id':identifier}).scalar_one()
    # A physical print created without a schedule has no economic identity;
    # linking it later must not manufacture historical authority.
    review_db.execute(text("INSERT INTO print_jobs(id,printer_id,job_name,status,started_at) VALUES(9999,9,'Legacy','failed',CURRENT_TIMESTAMP)"))
    review_db.execute(text('UPDATE print_jobs SET scheduled_job_id=:job WHERE id=9999'),{'job':job})
    review_db.commit()
    response=client.get(f'/orders/{order}/measured-costs',headers=auth('platform'))
    assert response.status_code==200,response.text
    value=response.json()
    assert value['filament_state']=='partial'
    assert value['known_attempts']==1 and value['unresolved_attempts']==1
    legacy=next(a for a in value['attempts'] if a['reason']=='missing_historical_valuation')
    assert legacy['attempt_id'] is None and legacy['amount'] is None


def test_reverification_failure_is_unresolved(terminal,monkeypatch):
    from modules.archives import order_measured_costs
    client,auth,_,_,_,order=terminal
    def refuse(*args):raise ValueError('retained basis differs')
    monkeypatch.setattr(order_measured_costs,'verify_retained_basis',refuse)
    response=client.get(f'/orders/{order}/measured-costs',headers=auth('platform'))
    assert response.status_code==200,response.text
    value=response.json()
    assert value['unresolved_attempts']==1 and value['measured_filament_known_subtotal'] is None
    assert value['attempts'][0]['reason']=='unresolved_purchase_evidence'
    assert value['attempts'][0]['attempt_id'] is None


def test_oversized_measurement_preserves_archive(archive_terminal):
    archive,engine,identifier,printer,_=archive_terminal
    with engine.begin() as c:
        c.execute(text('UPDATE print_jobs SET filament_used_g=1e20 WHERE id=:id'),{'id':identifier})
    assert archive.create_print_archive(identifier,printer,True) is not None
    with engine.connect() as c:
        assert c.execute(text('SELECT reason FROM filament_cost_events')).scalar_one()=='invalid_measurement'


def test_read_endpoint_emits_no_write_or_lock_statements(terminal,review_db):
    from sqlalchemy import event
    client,auth,_,_,_,order=terminal
    statements=[]
    engine=review_db.get_bind()
    def capture(conn,cursor,statement,parameters,context,executemany):statements.append(statement)
    event.listen(engine,'before_cursor_execute',capture)
    try:
        response=client.get(f'/orders/{order}/measured-costs',headers=auth('platform'))
        assert response.status_code==200,response.text
    finally:event.remove(engine,'before_cursor_execute',capture)
    assert not [s for s in statements if s.lstrip().upper().startswith(('UPDATE ','INSERT ','DELETE ','LOCK ')) or 'FOR UPDATE' in s.upper()]


@pytest.mark.parametrize('price,expected',[('0.0000005','0.000000'),('0.0000015','0.000002')])
def test_stored_cost_rounds_half_even_to_six_places(price,expected):
    assert calculate(Decimal('1'),price,'1')==expected


def test_fully_measured_requires_terminal_jobs_and_measured_grams():
    from modules.archives.order_measured_costs import summarize
    attempt=dict(attempt_id=1,outcome='completed',measurement_basis='printer_reported',amount='1.000000',currency='USD',reason=None)
    assert summarize([attempt],jobs_terminal=True)['filament_state']=='partial'
    assert summarize([attempt],jobs_terminal=False)['filament_state']=='partial'
