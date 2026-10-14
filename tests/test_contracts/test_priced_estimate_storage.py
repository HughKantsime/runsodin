"""Owned SQL-only fixtures: synthetic actor bindings, no request capability."""
import json
from uuid import uuid4
import pytest
from sqlalchemy import create_engine,event,text
from sqlalchemy.exc import IntegrityError
from core.schema.migrator import MigrationError
from core import priced_estimate_schema as s,priced_estimate_validation as v
from core.priced_job_estimate import calculate_priced_job_estimate

NOW='2026-10-13T12:00:00.000000Z'

def insert(db,table,row,replace=False):
    names=tuple(row)
    return db.execute(text(('INSERT OR REPLACE' if replace else 'INSERT')+' INTO '+table+'('+','.join(names)+') VALUES('+','.join(':'+n for n in names)+')'),row)

def update(db,table,row,where,params):
    return db.execute(text('UPDATE '+table+' SET '+','.join(n+'=:'+n for n in row)+' WHERE '+where),{**params,**row})

@pytest.fixture
def db():
    engine=create_engine('sqlite://')
    @event.listens_for(engine,'connect')
    def fk(connection,_):connection.execute('PRAGMA foreign_keys=ON')
    with engine.connect() as connection:
        connection.exec_driver_sql('BEGIN')
        connection.exec_driver_sql("CREATE TABLE cost_identities(identity_uid TEXT PRIMARY KEY NOT NULL,resource_kind TEXT NOT NULL,resource_id INTEGER NOT NULL,owner_scope TEXT NOT NULL,owner_key INTEGER NOT NULL,UNIQUE(identity_uid,resource_kind,owner_scope,owner_key))")
        s.apply(connection);connection.commit();connection.exec_driver_sql('BEGIN')
        yield connection
        connection.rollback()
    engine.dispose()


def bundle(currency='USD'):
    assumptions={name:'0' for name in s.ACTIVE_FIELDS}
    assumptions.update(printer_lifespan='1',parts_per_month='1')
    return dict(currency=currency,assumptions=assumptions,material_rates={'PLA':dict(currency=currency,cost_per_gram='2')})

def source(db,scope='org',key=1,uid=None,revision=1,payload=None):
    payload=bundle() if payload is None else payload
    row=dict(source_uid=uid or uuid4().hex,revision=revision,owner_scope=scope,owner_key=key,currency=payload['currency'],assumptions_json=v.canonical_json(payload),source_sha256=v.digest(payload),assumptions_kind='explicit_saved',actor_id=7,created_at=NOW)
    insert(db,s.SOURCE,row);return row


def select(db,src,scope='org',key=1,revision=1):
    row=dict(owner_scope=scope,owner_key=key,revision=revision,source_uid=src['source_uid'],source_revision=src['revision'],source_owner_scope=src['owner_scope'],source_owner_key=src['owner_key'],currency=src['currency'],actor_binding_uid=uuid4().hex,actor_id=7,command_nonce=uuid4().hex,expected_revision=revision-1,created_at=NOW)
    row['request_sha256']=v.digest(v.selection_command(row));insert(db,s.SELECTION_HISTORY,row)
    if revision==1:insert(db,s.SELECTION,{k:row[k] for k in ('owner_scope','owner_key','revision')})
    else:update(db,s.SELECTION,dict(revision=revision),'owner_scope=:scope AND owner_key=:key',{'scope':scope,'key':key})
    return row


def receipt(db,src,h,classification='job_estimate',job=None,revision=1,omit=(),register=True):
    job=job or str(uuid4())
    if revision==1 and register:insert(db,'cost_identities',dict(identity_uid=job,resource_kind='job',resource_id=10,owner_scope=h['owner_scope'],owner_key=h['owner_key']))
    inputs=dict(material_key='PLA',grams_per_run='1',hours_per_run='0',runs=3,payload_sha256=None,plate_index=None,piece_count=None,capacity=None,model_fingerprint=None,file_fingerprint=None)
    row=dict(receipt_uid=uuid4().hex,job_uid=job,job_kind='job',owner_scope=h['owner_scope'],owner_key=h['owner_key'],receipt_revision=revision,classification=classification,formula='per-run-cost-v1',rounding='HALF_EVEN',amount='6.00',runs=3,required_total_name='total',unknown_reason=None,selection_owner_scope=h['owner_scope'],selection_owner_key=h['owner_key'],selection_revision=h['revision'],input_sha256=v.digest(inputs),input_json=v.canonical_json(inputs),payload_sha256=None,plate_index=None,piece_count=None,capacity=None,created_at=NOW,**{n:h[n] for n in s.SOURCE_LINK})
    if classification=='unknown':
        for name in (*s.SOURCE_LINK,'amount','runs','required_total_name','selection_owner_scope','selection_owner_key','selection_revision'):row[name]=None
        row['unknown_reason']='missing_source'
    insert(db,s.RECEIPT,row)
    if classification!='unknown':
        prepared=json.loads(src['assumptions_json']);prepared['assumptions'].update(spool_cost='0',spool_weight='1')
        result=calculate_priced_job_estimate(prepared,material_key='PLA',grams_per_run='1',hours_per_run='0',runs=3)
        for component in result.components:
            if component.name not in omit:insert(db,s.COMPONENT,dict(receipt_uid=row['receipt_uid'],component_name=component.name,currency=component.currency,basis=component.basis,predecessor_name=s.PREDECESSOR[component.name],numerator=str(component.numerator),denominator=str(component.denominator)))
    return row


def reference(db,r,revision=1,clear=False):
    row=dict(job_uid=r['job_uid'],owner_scope=r['owner_scope'],owner_key=r['owner_key'],revision=revision,receipt_uid=None if clear else r['receipt_uid'],invalidation_reason='explicit_clear' if clear else None,current_input_sha256=None if clear else r['input_sha256'],actor_binding_uid=uuid4().hex,actor_id=7,command_nonce=uuid4().hex,expected_revision=revision-1,created_at=NOW)
    row['request_sha256']=v.digest(v.reference_command(row));insert(db,s.REFERENCE_HISTORY,row)
    fields=('job_uid','owner_scope','owner_key','revision','receipt_uid','invalidation_reason','current_input_sha256')
    if revision==1:insert(db,s.CURRENT,{n:row[n] for n in fields})
    else:update(db,s.CURRENT,{n:row[n] for n in fields if n!='job_uid'},'job_uid=:uid',{'uid':r['job_uid']})
    return row


def seed(db):
    src=source(db);h=select(db,src);return src,h


def test_complete_known_cycles_and_arithmetic(db):
    src,h=seed(db);r=receipt(db,src,h);reference(db,r);v.validate(db);db.commit()
    assert db.exec_driver_sql('PRAGMA foreign_key_check').all()==[]
    assert db.execute(text('SELECT amount FROM '+s.RECEIPT)).scalar_one()=='6.00'

@pytest.mark.parametrize('name',s.NAMES)
def test_every_missing_component_fails_real_commit_whole_rollback(db,name):
    src,h=seed(db);receipt(db,src,h,omit=(name,))
    with pytest.raises(IntegrityError):db.commit()
    db.connection.driver_connection.rollback();db.rollback()
    assert db.execute(text('SELECT count(*) FROM '+s.SOURCE)).scalar_one()==0
    assert db.execute(text('SELECT count(*) FROM '+s.RECEIPT)).scalar_one()==0


def test_unknown_has_no_components_or_money(db):
    src,h=seed(db);r=receipt(db,src,h,'unknown');reference(db,r);v.validate(db);db.commit()
    with pytest.raises(IntegrityError):insert(db,s.COMPONENT,dict(receipt_uid=r['receipt_uid'],component_name='total',currency='USD',basis='all_runs',predecessor_name='subtotal',numerator='0',denominator='1'))


def test_scenario_immutable_no_current_reference(db):
    src,h=seed(db);r=receipt(db,src,h,'scenario');v.validate(db)
    with pytest.raises(IntegrityError):reference(db,r)

@pytest.mark.parametrize('scope,key',[('org',1),('global',0)])
def test_positive_owner_global_or_org(db,scope,key):
    src=source(db,scope,key);h=select(db,src,scope,key);r=receipt(db,src,h);reference(db,r);v.validate(db)


def test_org_may_explicitly_select_global_but_not_foreign_org(db):
    src=source(db,'global',0);h=select(db,src);r=receipt(db,src,h);reference(db,r);v.validate(db)
    foreign=source(db,'org',2)
    with pytest.raises(IntegrityError):select(db,foreign,revision=2)

@pytest.mark.parametrize('mutation',[lambda b:b['assumptions'].update(packaging_cost='1e2'),lambda b:b['assumptions'].update(printer_lifespan='0'),lambda b:b['material_rates']['PLA'].update(currency='EUR'),lambda b:b['assumptions'].update(spool_weight='1000'),lambda b:b.update(extra='x')])
def test_source_closed_units_and_active_decimal_inputs(db,mutation):
    payload=bundle();mutation(payload)
    with pytest.raises(IntegrityError):source(db,payload=payload)


def test_source_revision_sequence_owner_and_exhaustion(db):
    src=source(db)
    with pytest.raises(IntegrityError):source(db,uid=src['source_uid'],revision=3)
    with pytest.raises(IntegrityError):source(db,'org',2,uid=src['source_uid'],revision=2)
    source(db,uid=src['source_uid'],revision=2)
    assert db.execute(text('SELECT count(*) FROM '+s.SOURCE)).scalar_one()==2


def test_selection_and_current_cas_clear_history_preserved(db):
    src,h=seed(db);r=receipt(db,src,h);reference(db,r);reference(db,r,2,True);v.validate(db)
    with pytest.raises(IntegrityError):update(db,s.CURRENT,dict(revision=1),'job_uid=:uid',{'uid':r['job_uid']})
    with pytest.raises(IntegrityError):update(db,s.SELECTION,dict(revision=3),'owner_key=1',{})
    select(db,src,revision=2);v.validate(db)

@pytest.mark.parametrize('recursive',[0,1])
def test_every_table_replace_update_delete_refused(db,recursive):
    db.exec_driver_sql('PRAGMA recursive_triggers='+str(recursive));src,h=seed(db);r=receipt(db,src,h);reference(db,r)
    for table in s.TABLES:
        row=dict(db.execute(text('SELECT * FROM '+table+' LIMIT 1')).mappings().one())
        with pytest.raises(IntegrityError):insert(db,table,row,True)
        with pytest.raises(IntegrityError):db.execute(text('DELETE FROM '+table))
        with pytest.raises(IntegrityError):db.execute(text('UPDATE '+table+' SET '+next(iter(row))+'='+next(iter(row))))
    v.validate(db)


def test_retained_arithmetic_and_hash_tamper_refused_without_guard(db):
    src,h=seed(db);r=receipt(db,src,h);reference(db,r)
    db.exec_driver_sql('DROP TRIGGER '+s.COMPONENT+'_update_guard')
    db.execute(text("UPDATE "+s.COMPONENT+" SET numerator='7' WHERE component_name='total'"))
    with pytest.raises(MigrationError):v.validate_data(db)


def test_source_digest_and_noncanonical_json_refused_independently(db):
    src=source(db);db.exec_driver_sql('DROP TRIGGER '+s.SOURCE+'_update_guard')
    db.execute(text('UPDATE '+s.SOURCE+" SET source_sha256='"+'a'*64+"'"))
    with pytest.raises(MigrationError):v.validate_data(db)


def test_schema_drift_guard_detected(db):
    db.exec_driver_sql('DROP TRIGGER '+s.SOURCE+'_delete_guard')
    with pytest.raises(MigrationError):v.validate(db)


def test_sql_source_exhaustion4096_and_validator_fetch4097(db):
    src=source(db)
    for revision in range(2,4097):source(db,uid=src['source_uid'],revision=revision)
    with pytest.raises(IntegrityError):source(db,uid=src['source_uid'],revision=4097)
    assert len(v._group(db,s.SOURCE,'source_uid=:uid',{'uid':src['source_uid']}))==4096
    # Owned corrupted-restore oracle only; deliberately missing guard/check then
    # restore enforcement before independent data validation. Never a writer API.
    db.exec_driver_sql('DROP TRIGGER '+s.SOURCE+'_insert_guard');db.exec_driver_sql('PRAGMA ignore_check_constraints=ON')
    row={**src,'revision':4097};insert(db,s.SOURCE,row);db.exec_driver_sql('PRAGMA ignore_check_constraints=OFF')
    with pytest.raises(MigrationError):v.validate_data(db)


def test_exact_receipt_selection_source_mismatch_refused(db):
    src,h=seed(db);other=source(db)
    altered={**h,'source_uid':other['source_uid']}
    with pytest.raises(IntegrityError):receipt(db,other,altered)


def test_selection_clear_next_revision_retained(db):
    src,h=seed(db)
    row={**h,'revision':2,'expected_revision':1,'command_nonce':uuid4().hex}
    for name in s.SOURCE_LINK:row[name]=None
    row['request_sha256']=v.digest(v.selection_command(row));insert(db,s.SELECTION_HISTORY,row)
    update(db,s.SELECTION,{'revision':2},'owner_scope=:scope AND owner_key=:key',{'scope':'org','key':1});v.validate(db)
    assert db.execute(text('SELECT count(*) FROM '+s.SELECTION_HISTORY)).scalar_one()==2


def test_reference_current_cannot_disagree_with_history(db):
    src,h=seed(db);r=receipt(db,src,h);history=reference(db,r)
    row=dict(db.execute(text('SELECT * FROM '+s.CURRENT)).mappings().one())
    db.exec_driver_sql('DROP TRIGGER '+s.CURRENT+'_update_guard')
    update(db,s.CURRENT,{'receipt_uid':None,'invalidation_reason':'explicit_clear','current_input_sha256':None},'job_uid=:uid',{'uid':r['job_uid']})
    with pytest.raises(MigrationError):v.validate_data(db)


def test_duplicate_actor_command_rejected_all_history_tables(db):
    src,h=seed(db)
    row={**h,'revision':2,'expected_revision':1};row['request_sha256']=v.digest(v.selection_command(row))
    with pytest.raises(IntegrityError):insert(db,s.SELECTION_HISTORY,row)
    r=receipt(db,src,h);ref=reference(db,r)
    row={**ref,'revision':2,'expected_revision':1};row['request_sha256']=v.digest(v.reference_command(row))
    with pytest.raises(IntegrityError):insert(db,s.REFERENCE_HISTORY,row)


def test_whole_caller_business_and_storage_rollback_no_hidden_commit(db):
    db.exec_driver_sql('CREATE TABLE owned_business(value INTEGER NOT NULL)');db.exec_driver_sql('INSERT INTO owned_business VALUES(1)')
    src,h=seed(db);r=receipt(db,src,h);reference(db,r)
    db.rollback();db.exec_driver_sql('BEGIN')
    for table in s.TABLES:assert db.execute(text('SELECT count(*) FROM '+table)).scalar_one()==0
    # DDL also belonged to caller transaction.
    assert db.execute(text("SELECT count(*) FROM sqlite_schema WHERE name='owned_business'")).scalar_one()==0


def test_currency_component_mismatch_and_wrong_predecessor_refused(db):
    src,h=seed(db);r=receipt(db,src,h,omit=('total',))
    row=dict(receipt_uid=r['receipt_uid'],component_name='total',currency='EUR',basis='all_runs',predecessor_name='subtotal',numerator='6',denominator='1')
    with pytest.raises(IntegrityError):insert(db,s.COMPONENT,row)
    row.update(currency='USD',predecessor_name='base')
    with pytest.raises(IntegrityError):insert(db,s.COMPONENT,row)


def test_repeat_installer_validates_without_changes_and_partial_refuses(db):
    s.apply(db);v.validate(db)
    db.exec_driver_sql('DROP TRIGGER '+s.SOURCE+'_insert_guard')
    with pytest.raises(MigrationError):s.apply(db)


def test_pg_literal_compilation_and_zero_binds():
    from sqlalchemy.dialects.postgresql.psycopg import dialect
    from psycopg._queries import _query2pg
    statements=[s.ddl(table,True,False) for table in s.TABLES]+['CREATE FUNCTION public.'+name+'() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public AS $$'+body+'$$' for name,body in s.pg_functions().items()]
    for statement in statements:
        compiled=text(statement).compile(dialect=dialect())
        assert compiled.params=={} and compiled.binds=={}
        parsed,formats,order,_=_query2pg(str(compiled).encode(),'utf8')
        assert parsed==statement.encode() and formats==[] and order is None


def test_all_active_formula_inputs_retained_without_spool_fallback(db):
    payload=bundle();payload['material_rates']['PLA']['cost_per_gram']='0.02'
    payload['assumptions'].update(post_processing_min='10',packing_min='5',support_min='15',hourly_rate='20',printer_wattage='200',electricity_rate='0.3',printer_cost='1000',printer_lifespan='1000',packaging_cost='2',other_costs='1',failure_rate='10',monthly_rent='100',parts_per_month='20')
    src=source(db,payload=payload);prepared=v.validate_source(src)
    expected=calculate_priced_job_estimate(prepared,material_key='PLA',grams_per_run='100',hours_per_run='2',runs=3)
    assert expected.amount=='71.50'
    assert set(json.loads(src['assumptions_json'])['assumptions'])==set(s.ACTIVE_FIELDS)

@pytest.mark.parametrize('field',(*s.SOURCE_LINK,'amount','runs','required_total_name','selection_owner_scope','selection_owner_key','selection_revision'))
def test_known_nullable_columns_cannot_escape_with_null(db,field):
    src,h=seed(db);r=receipt(db,src,h)
    row={**r,'receipt_uid':uuid4().hex,'receipt_revision':2,field:None}
    with pytest.raises(IntegrityError):insert(db,s.RECEIPT,row)


def test_reference_invalidated_owner_must_match_registry(db):
    src,h=seed(db);r=receipt(db,src,h)
    row=dict(job_uid=r['job_uid'],owner_scope='org',owner_key=2,revision=1,receipt_uid=None,invalidation_reason='explicit_clear',current_input_sha256=None,actor_binding_uid=uuid4().hex,actor_id=7,command_nonce=uuid4().hex,expected_revision=0,created_at=NOW)
    row['request_sha256']=v.digest(v.reference_command(row))
    with pytest.raises(IntegrityError):insert(db,s.REFERENCE_HISTORY,row)


def test_guardless_scenario_reference_restore_refused(db):
    src,h=seed(db);r=receipt(db,src,h,'scenario')
    db.exec_driver_sql('DROP TRIGGER '+s.REFERENCE_HISTORY+'_insert_guard');db.exec_driver_sql('DROP TRIGGER '+s.CURRENT+'_insert_guard')
    reference(db,r)
    with pytest.raises(MigrationError):v.validate_data(db)


def test_noncanonical_payload_and_command_hash_restore_refused(db):
    src,h=seed(db)
    db.exec_driver_sql('DROP TRIGGER '+s.SOURCE+'_update_guard')
    update(db,s.SOURCE,{'assumptions_json':json.dumps(bundle())},'source_uid=:uid',{'uid':src['source_uid']})
    with pytest.raises(MigrationError):v.validate_data(db)


def test_pg_body_has_independent_sequence_lock_and_public_relations():
    for table in (s.SOURCE,s.SELECTION_HISTORY,s.RECEIPT,s.REFERENCE_HISTORY):
        body=s.pg_functions()['odin_'+table+'_guard']
        assert body.index('pg_try_advisory_xact_lock')<body.index('COALESCE(MAX(')
        assert 'FROM public.'+table in body
    assert 'FROM public.cost_identities' in s.pg_functions()['odin_'+s.CURRENT+'_guard']


def test_unknown_input_malformed_boolean_is_not_silently_metadata(db):
    src,h=seed(db);r=receipt(db,src,h,'unknown')
    db.exec_driver_sql('DROP TRIGGER '+s.RECEIPT+'_update_guard')
    payload=json.loads(r['input_json']);payload['plate_index']=True
    update(db,s.RECEIPT,{'plate_index':1,'input_json':v.canonical_json(payload),'input_sha256':v.digest(payload)},'receipt_uid=:uid',{'uid':r['receipt_uid']})
    with pytest.raises(MigrationError):v.validate_data(db)


from tests.test_contracts.test_education_review_workflow import review_db


def test_genuine021_job_birth_to_receipt_components_and_current(review_db):
    from core.economic_identity import _add_created
    from modules.jobs.models import Job
    job=_add_created(review_db,Job(item_name='Owned prospective cost',quantity=3,charged_to_org_id=1),'job',owner_org_id=1)
    uid=review_db.execute(text('SELECT economic_uid FROM jobs WHERE id=:id'),{'id':job.id}).scalar_one()
    assert len(uid)==36 and str(__import__('uuid').UUID(uid))==uid
    db=review_db.connection();s.apply(db)
    src,h=seed(db);r=receipt(db,src,h,job=uid,register=False);reference(db,r);v.validate(db)
    assert db.execute(text('SELECT count(*) FROM '+s.COMPONENT)).scalar_one()==11
    assert db.execute(text('SELECT job_uid FROM '+s.CURRENT)).scalar_one()==uid
    review_db.commit()


def test_guardless_local_classification_and_nonce_shape_revalidated(db):
    src,h=seed(db);r=receipt(db,src,h,'unknown')
    db.exec_driver_sql('DROP TRIGGER '+s.RECEIPT+'_update_guard');db.exec_driver_sql('PRAGMA ignore_check_constraints=ON')
    update(db,s.RECEIPT,{'receipt_uid':'malformed'},'receipt_uid=:uid',{'uid':r['receipt_uid']})
    db.exec_driver_sql('PRAGMA ignore_check_constraints=OFF')
    with pytest.raises(MigrationError):v.validate_data(db)

@pytest.mark.parametrize('field,value',[('source_uid','x'*32),('owner_scope','foreign'),('owner_key',-1),('revision',0),('currency','BTC'),('created_at','2026-02-30T12:00:00.000000Z')])
def test_each_retained_source_local_scalar_shape_without_guards(db,field,value):
    src=source(db);db.exec_driver_sql('DROP TRIGGER '+s.SOURCE+'_update_guard');db.exec_driver_sql('PRAGMA ignore_check_constraints=ON')
    update(db,s.SOURCE,{field:value},'source_uid=:uid',{'uid':src['source_uid']});db.exec_driver_sql('PRAGMA ignore_check_constraints=OFF')
    with pytest.raises(MigrationError):v._local_checks(db)
    with pytest.raises(MigrationError):v.validate_data(db)

@pytest.mark.parametrize('field,value',[('classification','invalid'),('job_kind','spool'),('required_total_name','not_total')])
def test_retained_receipt_local_closed_classification_without_guard(db,field,value):
    src,h=seed(db);r=receipt(db,src,h,'unknown');db.exec_driver_sql('DROP TRIGGER '+s.RECEIPT+'_update_guard');db.exec_driver_sql('PRAGMA ignore_check_constraints=ON')
    changes={field:value}
    if field=='job_kind':
        uid=str(uuid4())
        insert(db,'cost_identities',dict(identity_uid=uid,resource_kind='spool',resource_id=20,owner_scope='org',owner_key=1))
        changes['job_uid']=uid
    update(db,s.RECEIPT,changes,'receipt_uid=:uid',{'uid':r['receipt_uid']});db.exec_driver_sql('PRAGMA ignore_check_constraints=OFF')
    with pytest.raises(MigrationError):v._local_checks(db)


def test_retained_component_basis_and_rational_shape_without_guard(db):
    src,h=seed(db);r=receipt(db,src,h);db.exec_driver_sql('DROP TRIGGER '+s.COMPONENT+'_update_guard');db.exec_driver_sql('PRAGMA ignore_check_constraints=ON')
    update(db,s.COMPONENT,{'basis':'piece','denominator':'0'},"receipt_uid=:uid AND component_name='material'",{'uid':r['receipt_uid']});db.exec_driver_sql('PRAGMA ignore_check_constraints=OFF')
    with pytest.raises(MigrationError):v._local_checks(db)


def test_hex32_job_uid_refused_even_when_matching_synthetic_registry(db):
    src,h=seed(db)
    with pytest.raises(IntegrityError):receipt(db,src,h,job=uuid4().hex)


def test_pg_trigger_definitions_emit_exact_canonical_event_order():
    expected={table+'_row_guard':f'CREATE TRIGGER {table}_row_guard BEFORE INSERT OR DELETE OR UPDATE ON public.{table} FOR EACH ROW EXECUTE FUNCTION public.odin_{table}_guard()' for table in s.TABLES}
    assert {name:sql for name,sql in s.pg_triggers().items() if name.endswith('_row_guard')}==expected
    assert len(expected)==7
