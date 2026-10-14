"""Owned actual PostgreSQL026 foundation; no creator/runtime activation."""
import os,secrets,tempfile
from pathlib import Path
from uuid import uuid4

def main():
 with tempfile.TemporaryDirectory(prefix='global-origin-owned-') as folder:
  credential=Path(folder)/'credential';credential.write_text(secrets.token_urlsafe(48));credential.chmod(0o600)
  os.environ.update(DATABASE_URL=os.environ['GLOBAL_ORIGIN_NATIVE_URL'],DATABASE_PASSWORD_FILE=str(credential),ODIN_DATA_DIR=folder,JWT_SECRET_KEY=secrets.token_urlsafe(48))
  from sqlalchemy import text
  from sqlalchemy.orm import Session
  from core.db import engine
  from core.schema import bootstrap_database
  from core import global_order_origin_schema as G
  from core.schema.migrator import MigrationError
  from core.economic_identity import _add_created
  from modules.orders.models import Order
  bootstrap_database(engine)
  with Session(engine) as db:
   live=_add_created(db,Order(),'order',owner_org_id=None,global_authority=True)
   deleted=_add_created(db,Order(),'order',owner_org_id=None,global_authority=True)
   org=_add_created(db,Order(org_id=7),'order',owner_org_id=7)
   expected={live.id:db.execute(text('SELECT economic_uid FROM orders WHERE id=:id'),{'id':live.id}).scalar_one(),deleted.id:db.execute(text('SELECT economic_uid FROM orders WHERE id=:id'),{'id':deleted.id}).scalar_one()}
   db.execute(text('DELETE FROM orders WHERE id=:id'),{'id':deleted.id});db.commit()
  # Actual transactional cutover failure must undo source DDL/history together.
  original_validate=G.validate
  G.validate=lambda c:(_ for _ in ()).throw(RuntimeError('Owned final cutover failure'))
  try:
   with engine.begin() as c:G.apply(c)
  except RuntimeError:pass
  else:raise AssertionError('Injected cutover did not fail')
  finally:G.validate=original_validate
  with engine.begin() as c:
   assert c.execute(text("SELECT to_regclass('public.cost_global_order_origins')")).scalar_one() is None
   assert c.execute(text("SELECT count(*) FROM information_schema.columns WHERE table_schema='public' AND table_name='orders' AND column_name='economic_global_origin_nonce'")).scalar_one()==0
   G.apply(c);G.apply(c);G.validate(c)
   assert set(c.execute(text('SELECT order_uid FROM cost_global_origin_exclusions')).scalars())==set(expected.values())
   assert c.execute(text('SELECT count(*) FROM cost_global_order_origins')).scalar_one()==0
  print('global-origin-native-cutover-deleted-roots-unused-adoption-rollback-pass')
  # Both acquisition orders: writer→cutover refuses, cutover→writer refuses;
  # rollback releases the same owned relation locks, with stricter bound retained.
  with engine.connect() as first,engine.connect() as second:
   first.exec_driver_sql('LOCK TABLE orders IN ROW EXCLUSIVE MODE')
   try:G.validate(second)
   except Exception as e:assert e.orig.sqlstate=='55P03';second.rollback()
   else:raise AssertionError('Cutover ignored active writer')
   first.rollback()
   G.validate(first)
   second.exec_driver_sql("SET LOCAL lock_timeout='50ms'")
   try:second.exec_driver_sql('UPDATE orders SET id=id WHERE false')
   except Exception as e:assert e.orig.sqlstate=='55P03';second.rollback()
   else:raise AssertionError('Writer ignored cutover fence')
   first.rollback()
   second.exec_driver_sql("SET LOCAL lock_timeout='50ms'");G.validate(second)
   assert second.execute(text("SELECT setting FROM pg_settings WHERE name='lock_timeout'")).scalar_one()=='50'
   second.rollback()
   with second.begin():second.exec_driver_sql('UPDATE orders SET id=id WHERE false')
  print('global-origin-native-both-lock-orders-nowait-stricter-bound-release-pass')
  def fresh(c,nonce=None,id=None):
   nonce=nonce or uuid4().hex;uid=str(uuid4())
   identifier=c.execute(text('INSERT INTO orders(economic_global_origin_nonce'+(',id' if id else '')+') VALUES(:n'+(',:id' if id else '')+') RETURNING id'),{'n':nonce,'id':id}).scalar_one()
   c.execute(text("INSERT INTO cost_identities(identity_uid,resource_kind,resource_id,owner_scope,owner_key) VALUES(:u,'order',:id,'global',0)"),{'u':uid,'id':identifier})
   c.execute(text('UPDATE orders SET economic_uid=:u WHERE id=:id'),{'u':uid,'id':identifier})
   c.execute(text("INSERT INTO cost_global_order_origins(order_uid,resource_kind,owner_scope,owner_key,origin_nonce,proof_kind) VALUES(:u,'order','global',0,:n,'verified_platform_admin_global_creation')"),{'u':uid,'n':nonce})
   return identifier,uid,nonce
  with engine.begin() as c:
   id,uid,nonce=fresh(c);G.validate(c)
   try:G.apply(c)
   except MigrationError:pass
   else:raise AssertionError('Used unrecorded footprint adopted')
  for sql in ('UPDATE cost_global_order_origins SET owner_key=0','DELETE FROM cost_global_order_origins','TRUNCATE cost_global_order_origins CASCADE','UPDATE cost_global_origin_exclusions SET owner_key=0','DELETE FROM cost_global_origin_exclusions','TRUNCATE cost_global_origin_exclusions CASCADE','UPDATE orders SET economic_global_origin_nonce=NULL WHERE id=:id','INSERT INTO orders(id) VALUES(:id)'):
   with engine.connect() as c:
    try:c.execute(text(sql),{'id':id})
    except Exception:c.rollback()
    else:raise AssertionError('History mutation accepted')
  with engine.begin() as c:c.execute(text('DELETE FROM orders WHERE id=:id'),{'id':id});G.validate(c)
  with engine.begin() as c:
   id2,uid2,_=fresh(c,id=id);assert id2==id and uid2!=uid;G.validate(c)
  with engine.connect() as c:
   try:fresh(c,nonce=nonce)
   except Exception as e:assert e.orig.sqlstate=='23514';c.rollback()
   else:raise AssertionError('Retired nonce accepted')
  print('global-origin-native-positive-immutable-history-numeric-reuse-pass')
  for table in G.TABLES:
   for alteration in ('foreign','delete','update','deferred','notvalid','columns'):
    with engine.connect() as c:
     tx=c.begin();c.exec_driver_sql('ALTER TABLE public.'+table+' DROP CONSTRAINT go_identity')
     target='public.cost_identities';local='order_uid,resource_kind,owner_scope,owner_key';remote='identity_uid,resource_kind,owner_scope,owner_key'
     if alteration=='foreign':
      c.exec_driver_sql('CREATE SCHEMA foreign_origin');c.exec_driver_sql('CREATE TABLE foreign_origin.cost_identities(LIKE public.cost_identities INCLUDING ALL)');c.exec_driver_sql('INSERT INTO foreign_origin.cost_identities SELECT * FROM public.cost_identities');target='foreign_origin.cost_identities'
     if alteration=='columns':local='resource_kind,order_uid,owner_scope,owner_key';remote='resource_kind,identity_uid,owner_scope,owner_key'
     c.exec_driver_sql('ALTER TABLE public.'+table+' ADD CONSTRAINT go_identity FOREIGN KEY('+local+') REFERENCES '+target+'('+remote+') ON UPDATE '+('CASCADE' if alteration=='update' else 'RESTRICT')+' ON DELETE '+('NO ACTION' if alteration=='delete' else 'RESTRICT')+(' DEFERRABLE INITIALLY DEFERRED' if alteration=='deferred' else '')+(' NOT VALID' if alteration=='notvalid' else ''))
     try:G.validate(c)
     except MigrationError:tx.rollback()
     else:tx.rollback();raise AssertionError('FK drift adopted')
  for sql in ('ALTER TABLE cost_global_order_origins DROP CONSTRAINT go_nonce','ALTER TABLE orders DISABLE TRIGGER orders_global_origin_guard','ALTER FUNCTION cost_global_order_origins_guard() SECURITY DEFINER','CREATE UNIQUE INDEX orders_unknown_route ON orders(order_number)'):
   with engine.connect() as c:
    tx=c.begin();c.exec_driver_sql(sql)
    try:G.validate(c)
    except MigrationError:tx.rollback()
    else:tx.rollback();raise AssertionError('Executable/key drift adopted')
  print('global-origin-native-exact-public-FK-constraints-executable-negatives-pass')
  # Exact same retained UID is the serialization anchor across both ledgers.
  with engine.begin() as c:
   pending_nonce=uuid4().hex;pending_uid=str(uuid4())
   pending_id=c.execute(text('INSERT INTO orders(economic_global_origin_nonce) VALUES(:n) RETURNING id'),{'n':pending_nonce}).scalar_one()
   c.execute(text("INSERT INTO cost_identities VALUES(:u,'order',:id,'global',0)"),{'u':pending_uid,'id':pending_id})
   c.execute(text('UPDATE orders SET economic_uid=:u WHERE id=:id'),{'u':pending_uid,'id':pending_id})
  origin_sql=text("INSERT INTO cost_global_order_origins(order_uid,resource_kind,owner_scope,owner_key,origin_nonce,proof_kind) VALUES(:u,'order','global',0,:n,'verified_platform_admin_global_creation')")
  exclusion_sql=text("INSERT INTO cost_global_origin_exclusions VALUES(:u,'order','global',0)")
  for winner,loser in ((origin_sql,exclusion_sql),(exclusion_sql,origin_sql)):
   with engine.connect() as first,engine.connect() as second:
    first.execute(winner,{'u':pending_uid,'n':pending_nonce})
    try:second.execute(loser,{'u':pending_uid,'n':pending_nonce})
    except Exception as e:assert e.orig.sqlstate=='55P03';second.rollback()
    else:raise AssertionError('Simultaneous cross-ledger admission did not serialize')
    first.rollback()
  with engine.begin() as c:c.execute(origin_sql,{'u':pending_uid,'n':pending_nonce});G.validate(c)
  print('global-origin-native-cross-ledger-same-UID-both-insert-orders-serialize-pass')

  for value in (None,'f'*32):
   with engine.connect() as c:
    tx=c.begin();c.exec_driver_sql('DROP TRIGGER orders_global_origin_guard ON orders')
    c.execute(text('UPDATE orders SET economic_global_origin_nonce=:n WHERE id=:id'),{'n':value,'id':id})
    c.exec_driver_sql(G.pg_triggers()[('orders','orders_global_origin_guard')])
    try:G.validate(c)
    except MigrationError:tx.rollback()
    else:tx.rollback();raise AssertionError('Bidirectional source drift adopted')
  with engine.connect() as c:
   tx=c.begin();before=c.execute(text('SELECT count(*) FROM cost_global_order_origins')).scalar_one();fresh(c);tx.rollback()
   assert c.execute(text('SELECT count(*) FROM cost_global_order_origins')).scalar_one()==before
  with engine.begin() as c:G.validate(c)
  print('global-origin-native-bidirectional-recorded-validation-creator-rollback-pass')
  engine.dispose()
 print('global-origin-native-pass')
if __name__=='__main__':main()
