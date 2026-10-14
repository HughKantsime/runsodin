"""Owned025 foundation proof with actual PostgreSQL, no source valuation/callbacks."""
import os,secrets,tempfile,importlib
from pathlib import Path

def main():
 with tempfile.TemporaryDirectory(prefix='purchase-native-owned-') as folder:
  root=Path(folder);root.chmod(0o700);credential=root/'credential';credential.write_text(secrets.token_urlsafe(48));credential.chmod(0o600)
  os.environ.update(DATABASE_URL=os.environ['SPOOL_PURCHASE_NATIVE_URL'],DATABASE_PASSWORD_FILE=str(credential),ODIN_DATA_DIR=folder,JWT_SECRET_KEY=secrets.token_urlsafe(48))
  from sqlalchemy import text
  from sqlalchemy.orm import Session
  from core.db import engine
  from core.schema import bootstrap_database
  from core.economic_source_creation import _create_spool
  from core.spool_purchase_evidence import write_purchase,read_purchase
  from core.spool_purchase_schema import pg_functions,pg_triggers
  from core.schema.migrator import MigrationError
  M=importlib.import_module('core.schema.migrations.025_spool_purchase_evidence')
  bootstrap_database(engine)
  with engine.begin() as c:
   c.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(80,'Owned','Fixture','PLA')"));M.apply(c);M.validate(c);M.apply(c)
  actor={'id':3,'role':'admin','group_id':1,'_auth_kind':'bearer_jwt'}
  basis={'currency':'USD','price':'00012.3400','net_weight_g':'01000.00'}
  with Session(engine) as db:
   db.execute(text('SELECT 1'));made=_create_spool(db,{'filament_id':80,'org_id':1,'price':10,'initial_weight_g':1000,'remaining_weight_g':1000,'status':'active'},owner_org_id=1);db.commit();id=made.id
   write_purchase(db,id,actor,basis,0);db.commit();first=read_purchase(db,id,actor)
   assert (first['price_decimal'],first['net_weight_g_decimal'],first['revision'])==('12.34','1000',1)
   db.execute(text('UPDATE spools SET price=11 WHERE id=:id'),{'id':id});db.commit();assert read_purchase(db,id,actor)=={'state':'unknown','revision':2,'can_write':True}
   write_purchase(db,id,actor,dict(basis,price='0',currency='EUR'),2,legacy_fields={'price':0});db.commit()
   assert read_purchase(db,id,actor)['revision']==3 and read_purchase(db,id,actor)['price_decimal']=='0'
   write_purchase(db,id,actor,None,3);db.commit()
   for nonce,revision in [(first['evidence_nonce'],1),(first['evidence_nonce'],5),(None,None),(None,3),(None,6)]:
    try:db.execute(text('UPDATE spools SET purchase_evidence_nonce=:n,purchase_revision=:r WHERE id=:id'),{'n':nonce,'r':revision,'id':id})
    except Exception as error:assert error.orig.sqlstate=='23514';db.rollback()
    else:raise AssertionError('Purchase transition bypass')
   assert read_purchase(db,id,actor)=={'state':'unknown','revision':4,'can_write':True}
   assert read_purchase(db,id,{'role':'admin','group_id':2})=={'state':'unavailable','can_write':False}
  for sql in ('UPDATE spool_purchase_evidence SET currency=\'GBP\'','DELETE FROM spool_purchase_evidence','TRUNCATE spool_purchase_evidence','TRUNCATE spool_purchase_evidence CASCADE'):
   with engine.connect() as c:
    try:c.execute(text(sql))
    except Exception as error:
     assert error.orig.sqlstate==('0A000' if sql=='TRUNCATE spool_purchase_evidence' else '23514')
     c.rollback()
    else:raise AssertionError('Purchase history mutation accepted')
  for sql in ('ALTER TABLE spool_purchase_evidence DROP CONSTRAINT spe_price','ALTER TABLE spool_purchase_evidence DISABLE TRIGGER spool_purchase_evidence_guard','ALTER FUNCTION spool_purchase_evidence_guard() SECURITY DEFINER'):
   with engine.begin() as c:
    nested=c.begin_nested();c.execute(text(sql))
    try:M.validate(c)
    except MigrationError:nested.rollback()
    else:raise AssertionError('Purchase drift adopted')
  # Owned alternate-schema FK adoption probes, rolled back individually.
  targets=[('spool_purchase_evidence','spe_birth','cost_source_lifetimes','birth_nonce','birth_nonce'),
           ('spool_purchase_evidence','spe_identity','cost_identities','spool_uid,resource_kind,owner_scope,owner_key','identity_uid,resource_kind,owner_scope,owner_key'),
           ('spools','spools_purchase_evidence_nonce_fkey','spool_purchase_evidence','purchase_evidence_nonce','evidence_nonce')]
  for table,constraint,target,local,remote in targets:
   with engine.connect() as c:
    transaction=c.begin()
    c.exec_driver_sql('CREATE SCHEMA purchase_foreign')
    c.exec_driver_sql('CREATE TABLE purchase_foreign.'+target+' (LIKE public.'+target+' INCLUDING ALL)')
    c.exec_driver_sql('INSERT INTO purchase_foreign.'+target+' SELECT * FROM public.'+target)
    c.exec_driver_sql('ALTER TABLE public.'+table+' DROP CONSTRAINT '+constraint)
    c.exec_driver_sql('ALTER TABLE public.'+table+' ADD CONSTRAINT '+constraint+' FOREIGN KEY('+local+') REFERENCES purchase_foreign.'+target+'('+remote+') ON DELETE RESTRICT')
    try:M.apply(c)
    except MigrationError:transaction.rollback()
    else:
     transaction.rollback();raise AssertionError('Foreign-schema purchase FK adopted: '+constraint)
  alterations=[
   "ALTER TABLE public.spool_purchase_evidence DROP CONSTRAINT spe_birth; ALTER TABLE public.spool_purchase_evidence ADD CONSTRAINT spe_birth FOREIGN KEY(birth_nonce) REFERENCES public.cost_source_lifetimes(birth_nonce) ON DELETE NO ACTION",
   "ALTER TABLE public.spool_purchase_evidence DROP CONSTRAINT spe_birth; ALTER TABLE public.spool_purchase_evidence ADD CONSTRAINT spe_birth FOREIGN KEY(birth_nonce) REFERENCES public.cost_source_lifetimes(birth_nonce) ON UPDATE CASCADE ON DELETE RESTRICT",
   "ALTER TABLE public.spool_purchase_evidence DROP CONSTRAINT spe_birth; ALTER TABLE public.spool_purchase_evidence ADD CONSTRAINT spe_birth FOREIGN KEY(birth_nonce) REFERENCES public.cost_source_lifetimes(birth_nonce) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED",
   "ALTER TABLE public.spool_purchase_evidence DROP CONSTRAINT spe_birth; ALTER TABLE public.spool_purchase_evidence ADD CONSTRAINT spe_birth FOREIGN KEY(birth_nonce) REFERENCES public.cost_source_lifetimes(birth_nonce) ON DELETE RESTRICT NOT VALID",
   "ALTER TABLE public.spool_purchase_evidence DROP CONSTRAINT spe_identity; ALTER TABLE public.spool_purchase_evidence ADD CONSTRAINT spe_identity FOREIGN KEY(resource_kind,spool_uid,owner_scope,owner_key) REFERENCES public.cost_identities(resource_kind,identity_uid,owner_scope,owner_key) ON DELETE RESTRICT",
   "ALTER TABLE public.spools DROP CONSTRAINT spools_purchase_evidence_nonce_fkey; ALTER TABLE public.spools ADD CONSTRAINT spools_purchase_evidence_nonce_fkey FOREIGN KEY(purchase_evidence_nonce) REFERENCES public.spool_purchase_evidence(evidence_nonce) ON DELETE NO ACTION",
  ]
  for statement in alterations:
   with engine.connect() as c:
    transaction=c.begin()
    for part in statement.split(';'):c.exec_driver_sql(part)
    try:M.apply(c)
    except MigrationError:transaction.rollback()
    else:
     transaction.rollback();raise AssertionError('Altered purchase FK adopted')
  for counter in (None,1,3,4):
   with engine.connect() as c:
    transaction=c.begin()
    c.exec_driver_sql('DROP TRIGGER spools_purchase_guard ON public.spools')
    c.execute(text('UPDATE spools SET purchase_revision=:counter WHERE id=:id'),{'counter':counter,'id':id})
    c.exec_driver_sql(pg_triggers()[('spools','spools_purchase_guard')])
    try:M.apply(c)
    except MigrationError:
     assert counter is None or counter<3
    else:assert counter is not None and counter>=3,'Retained purchase counter rewind adopted'
    finally:transaction.rollback()
  print('spool-purchase-native-public-fk-counter-adoption-pass')
  with engine.begin() as c:M.validate(c)
  print('spool-purchase-native-foundation-transition-canonical-owner-immutability-drift-pass')
  print('spool-purchase-native-pg-functions='+repr(sorted(pg_functions())))
  print('spool-purchase-native-pg-triggers='+repr(sorted(pg_triggers())))
  engine.dispose()
 print('spool-purchase-native-pass')
if __name__=='__main__':main()
