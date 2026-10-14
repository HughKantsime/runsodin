"""025 explicit fixtures; raw purchase transitions and exact input boundaries."""
import importlib
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db
from core.economic_source_creation import _create_spool
from core.spool_purchase_evidence import write_purchase,read_purchase
from core.spool_purchase_validation import PurchaseInput,decimal_input,MAX_REVISION
from core.schema.migrator import MigrationError
M=importlib.import_module('core.schema.migrations.025_spool_purchase_evidence')
actor={'id':3,'role':'admin','group_id':1,'_auth_kind':'bearer_jwt'}

@pytest.fixture(autouse=True)
def prior024_standalone_fixture(monkeypatch):
    # Keep these explicit foundation/adoption fixtures at their original024
    # starting point now that main bootstrap registers025. Separate registered
    # tests exercise the real unmodified025 bootstrap.
    import core.schema.bootstrap as bootstrap
    real=bootstrap.apply_python_migration
    monkeypatch.setattr(bootstrap,'apply_python_migration',lambda c,name:False if name.endswith('.025_spool_purchase_evidence') else real(c,name))
    monkeypatch.setattr(bootstrap,'RAW_REQUIRED_TABLES',bootstrap.RAW_REQUIRED_TABLES-{'spool_purchase_evidence'})
    columns=dict(bootstrap.RAW_REQUIRED_COLUMNS);columns.pop('spool_purchase_evidence')
    columns['spools']=columns['spools']-{'purchase_evidence_nonce','purchase_revision'}
    monkeypatch.setattr(bootstrap,'RAW_REQUIRED_COLUMNS',columns)


@pytest.fixture
def purchase_db(review_db):
    review_db.commit()
    with review_db.get_bind().begin() as c:
        c.exec_driver_sql('BEGIN IMMEDIATE');M.apply(c)
        c.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(80,'Owned','Fixture','PLA')"))
    return review_db

def source(db):
    db.execute(text('SELECT 1'))
    made=_create_spool(db,{'filament_id':80,'org_id':1,'price':10,'initial_weight_g':1000,'remaining_weight_g':1000,'status':'active'},owner_org_id=1)
    db.commit();return made.id

def state(db,id):return db.execute(text('SELECT purchase_evidence_nonce,purchase_revision,price,initial_weight_g FROM spools WHERE id=:id'),{'id':id}).one()

def basis(**kwargs):return {'currency':'USD','price':'00012.3400','net_weight_g':'01000.00',**kwargs}

def test_normalization_examples():
    assert decimal_input('00012.3400')=='12.34'
    assert decimal_input('000.000')=='0'
    assert decimal_input('000.00100',positive=True)=='0.001'
    with pytest.raises(ValueError):decimal_input('000.000',positive=True)

@pytest.mark.parametrize('value',[1,1.5,True,None,'NaN','Infinity','1e3',' 1','1 ','+1','-1','１','0.1234567890123','9'*19])
def test_strict_input_rejects_before_decimal(value):
    with pytest.raises((ValueError,TypeError)):PurchaseInput.model_validate(basis(price=value))

@pytest.mark.parametrize('recursive',[False,True])
def test_record_replace_partial_invalidation_clear_exact_once(purchase_db,recursive):
    db=purchase_db;db.execute(text('PRAGMA recursive_triggers='+('ON' if recursive else 'OFF')));id=source(db)
    assert state(db,id)[:2]==(None,None)
    assert write_purchase(db,id,actor,basis(),0)==1;db.commit()
    first=state(db,id)[0];r=read_purchase(db,id,actor);assert (r['revision'],r['price_decimal'],r['net_weight_g_decimal'])==(1,'12.34','1000')
    db.execute(text('UPDATE spools SET price=11 WHERE id=:id'),{'id':id});db.commit()
    assert state(db,id)[:2]==(None,2)
    db.execute(text('UPDATE spools SET price=11 WHERE id=:id'),{'id':id});db.commit();assert state(db,id)[1]==2
    assert write_purchase(db,id,actor,basis(price='0'),2)==3;db.commit()
    assert read_purchase(db,id,actor)['price_decimal']=='0'
    db.execute(text('UPDATE spools SET remaining_weight_g=3,spool_weight_g=5 WHERE id=:id'),{'id':id});db.commit();assert state(db,id)[1]==3
    write_purchase(db,id,actor,None,3);db.commit();assert state(db,id)[:2]==(None,4)
    for nonce,rev in [(first,1),(first,5),(None,None),(None,3),(None,6)]:
        with pytest.raises(Exception):db.execute(text('UPDATE spools SET purchase_evidence_nonce=:n,purchase_revision=:r WHERE id=:id'),{'n':nonce,'r':rev,'id':id})
        db.rollback()
    assert state(db,id)[:2]==(None,4)
    assert db.execute(text('SELECT count(*) FROM spool_purchase_evidence')).scalar_one()==2

@pytest.mark.parametrize('recursive',[False,True])
def test_atomic_full_replacement_with_legacy_edit_not_doubleinvalidate(purchase_db,recursive):
    db=purchase_db;db.execute(text('PRAGMA recursive_triggers='+('ON' if recursive else 'OFF')));id=source(db)
    write_purchase(db,id,actor,basis(),0);db.commit()
    write_purchase(db,id,actor,basis(currency='EUR',price='0'),1,legacy_fields={'price':0});db.commit()
    assert state(db,id)[1]==2 and state(db,id)[0] is not None and state(db,id)[2]==0
    assert read_purchase(db,id,actor)['currency']=='EUR'

@pytest.mark.parametrize('recursive',[False,True])
def test_immutability_replace_recursion_modes(purchase_db,recursive):
    db=purchase_db;db.execute(text('PRAGMA recursive_triggers='+('ON' if recursive else 'OFF')));id=source(db);write_purchase(db,id,actor,basis(),0);db.commit()
    for sql in ["UPDATE spool_purchase_evidence SET currency='EUR'",'DELETE FROM spool_purchase_evidence',
        'INSERT OR REPLACE INTO spool_purchase_evidence SELECT * FROM spool_purchase_evidence']:
        with pytest.raises(Exception):db.execute(text(sql))
        db.rollback()
    assert read_purchase(db,id,actor)['revision']==1

def test_unknown_reads_disclose_no_revision_or_basis(purchase_db):
    db=purchase_db;id=source(db);write_purchase(db,id,actor,basis(),0);db.commit()
    assert read_purchase(db,id,{'role':'operator','group_id':2})=={'state':'unavailable','can_write':False}
    with pytest.raises(Exception):write_purchase(db,id,{'role':'operator','group_id':2},basis(),1)
    db.rollback();assert read_purchase(db,id,actor)['revision']==1

def test_helper_rollback_and_stale_expected(purchase_db,monkeypatch):
    db=purchase_db;id=source(db);original=Session.execute
    def refuse(self,statement,*a,**kw):
        if str(statement).startswith('UPDATE spools SET purchase_evidence_nonce='):raise RuntimeError('Owned pointer failure')
        return original(self,statement,*a,**kw)
    monkeypatch.setattr(Session,'execute',refuse)
    with pytest.raises(RuntimeError):write_purchase(db,id,actor,basis(),0)
    db.commit();assert state(db,id)[:2]==(None,None)
    assert db.execute(text('SELECT count(*) FROM spool_purchase_evidence')).scalar_one()==0
    monkeypatch.setattr(Session,'execute',original)
    write_purchase(db,id,actor,basis(),0);db.commit()
    with pytest.raises(Exception):write_purchase(db,id,actor,basis(),0)
    db.rollback();assert read_purchase(db,id,actor)['revision']==1

def test_exact_adoption_and_partial_trigger_refusal(purchase_db):
    db=purchase_db;db.commit()
    with db.get_bind().begin() as c:c.exec_driver_sql('BEGIN IMMEDIATE');M.apply(c)
    with db.get_bind().begin() as c:c.exec_driver_sql('DROP TRIGGER spools_purchase_invalidate')
    with pytest.raises(MigrationError):
        with db.get_bind().begin() as c:c.exec_driver_sql('BEGIN IMMEDIATE');M.apply(c)

@pytest.mark.parametrize('recursive',[False,True])
def test_restored_max_revision_rejects_entire_financial_mutation(review_db,recursive):
    from core.spool_purchase_schema import ddl,sqlite_triggers
    db=review_db
    db.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(80,'Owned','Fixture','PLA')"));db.commit()
    id=source(db)
    uid,birth=db.execute(text('SELECT economic_uid,economic_birth_nonce FROM spools WHERE id=:id'),{'id':id}).one();db.commit()
    # Restore-style owned fixture loads boundary data before installing new025
    # guards; no installed constraint/trigger is disabled or dropped.
    with db.get_bind().begin() as c:
        c.exec_driver_sql('BEGIN IMMEDIATE');c.exec_driver_sql(ddl(False))
        c.exec_driver_sql('ALTER TABLE spools ADD COLUMN purchase_evidence_nonce TEXT REFERENCES spool_purchase_evidence(evidence_nonce) ON DELETE RESTRICT')
        c.exec_driver_sql("ALTER TABLE spools ADD COLUMN purchase_revision INTEGER CONSTRAINT spools_purchase_revision_check CHECK(purchase_revision IS NULL OR (typeof(purchase_revision)='integer' AND purchase_revision BETWEEN 1 AND 2147483647))")
        c.execute(text("INSERT INTO spool_purchase_evidence(evidence_nonce,spool_uid,birth_nonce,owner_scope,owner_key,revision,currency,price_decimal,net_weight_g_decimal) VALUES(:n,:uid,:birth,'org',1,:max,'USD','10','1000')"),{'n':'a'*32,'uid':uid,'birth':birth,'max':MAX_REVISION})
        c.execute(text('UPDATE spools SET purchase_evidence_nonce=:n,purchase_revision=:max WHERE id=:id'),{'n':'a'*32,'max':MAX_REVISION,'id':id})
        for sql in sqlite_triggers().values():c.exec_driver_sql(sql)
        M.validate(c)
    db.execute(text('PRAGMA recursive_triggers='+('ON' if recursive else 'OFF')))
    with pytest.raises(Exception):db.execute(text('UPDATE spools SET price=99,remaining_weight_g=4 WHERE id=:id'),{'id':id})
    db.rollback()
    assert state(db,id)==('a'*32,MAX_REVISION,10,1000)
    assert db.execute(text('SELECT remaining_weight_g FROM spools WHERE id=:id'),{'id':id}).scalar_one()==1000
    with pytest.raises(Exception):write_purchase(db,id,actor,basis(),MAX_REVISION)
    db.rollback();assert state(db,id)[1]==MAX_REVISION

@pytest.mark.parametrize('counter',[None,1,2,3])
def test_adoption_counter_cannot_precede_retained_history(review_db,counter):
    from core.spool_purchase_schema import ddl,sqlite_triggers
    db=review_db
    db.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(80,'Owned','Fixture','PLA')"));db.commit()
    id=source(db)
    uid,birth=db.execute(text('SELECT economic_uid,economic_birth_nonce FROM spools WHERE id=:id'),{'id':id}).one();db.commit()
    # Exact restored schema/data before installing guards; no guard is disabled.
    with db.get_bind().begin() as c:
        c.exec_driver_sql('BEGIN IMMEDIATE');c.exec_driver_sql(ddl(False))
        c.exec_driver_sql('ALTER TABLE spools ADD COLUMN purchase_evidence_nonce TEXT REFERENCES spool_purchase_evidence(evidence_nonce) ON DELETE RESTRICT')
        c.exec_driver_sql("ALTER TABLE spools ADD COLUMN purchase_revision INTEGER CONSTRAINT spools_purchase_revision_check CHECK(purchase_revision IS NULL OR (typeof(purchase_revision)='integer' AND purchase_revision BETWEEN 1 AND 2147483647))")
        c.execute(text("INSERT INTO spool_purchase_evidence(evidence_nonce,spool_uid,birth_nonce,owner_scope,owner_key,revision,currency,price_decimal,net_weight_g_decimal) VALUES(:n,:uid,:birth,'org',1,2,'USD','0','1000')"),{'n':'b'*32,'uid':uid,'birth':birth})
        c.execute(text('UPDATE spools SET purchase_revision=:counter WHERE id=:id'),{'counter':counter,'id':id})
        for sql in sqlite_triggers().values():c.exec_driver_sql(sql)
    with db.get_bind().begin() as c:
        c.exec_driver_sql('BEGIN IMMEDIATE')
        if counter is None or counter<2:
            with pytest.raises(MigrationError,match='Retained purchase counter'):M.apply(c)
        else:M.apply(c)

@pytest.mark.parametrize('change',['delete_action','update_action','foreign_target'])
def test_sqlite_adoption_rejects_changed_evidence_fk_contract(purchase_db,change):
    from core.spool_purchase_schema import ddl,sqlite_triggers
    db=purchase_db;db.commit()
    contract=ddl(False)
    if change=='delete_action':contract=contract.replace('ON DELETE RESTRICT','ON DELETE NO ACTION',1)
    elif change=='update_action':contract=contract.replace('ON DELETE RESTRICT','ON UPDATE CASCADE ON DELETE RESTRICT',1)
    else:contract=contract.replace('REFERENCES cost_source_lifetimes(birth_nonce)','REFERENCES purchase_alias(birth_nonce)',1)
    with db.get_bind().begin() as c:
        c.exec_driver_sql('BEGIN IMMEDIATE')
        if change=='foreign_target':c.exec_driver_sql('CREATE TABLE purchase_alias(birth_nonce TEXT PRIMARY KEY)')
        c.exec_driver_sql('DROP TABLE spool_purchase_evidence');c.exec_driver_sql(contract)
        for name,sql in sqlite_triggers().items():
            if name.startswith('spool_purchase_evidence_'):c.exec_driver_sql(sql)
    with pytest.raises(MigrationError,match='Purchase table contract'):
        with db.get_bind().begin() as c:c.exec_driver_sql('BEGIN IMMEDIATE');M.apply(c)
