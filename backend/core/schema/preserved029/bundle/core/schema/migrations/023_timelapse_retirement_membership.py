"""Empty lifetime retirement membership; strict adoption, no media action."""
import hashlib,importlib
from pathlib import Path
from sqlalchemy import inspect,text
from core.schema.migrator import MigrationError
from core.timelapse_retirement_membership_schema import TABLES
from core.timelapse_retirement_membership_validation import sqlite_triggers,pg_functions,pg_triggers,validate_retained
MIGRATION_ID='python:023-timelapse-retirement-membership'
SOURCE_PINS={'timelapse_retirement_membership_schema.py': 'b4f86a2c8f1dfac940bbf8ff8764f17e05635bdbe3c4b802197f84cae45aff2c', 'timelapse_retirement_membership_validation.py': '2c6e5f23ff8a04f251304b46284f0b306ecbd0060a2fc2de9a6234a2eeca2a1d'}
_previous=importlib.import_module('core.schema.migrations.019_timelapse_publications')
def _source():
 for name,pin in SOURCE_PINS.items():
  if hashlib.sha256((Path(__file__).resolve().parents[2]/name).read_bytes()).hexdigest()!=pin:raise MigrationError('Membership source differs')
def _normal(sql):return ' '.join(sql.split()).strip().rstrip(';')
def _boundary(c):
 _previous._boundary(c)
 if c.dialect.name=='postgresql':
  if c.exec_driver_sql('SELECT current_schema()').scalar_one()!='public':raise MigrationError('Membership schema differs')
  for table in TABLES:
   exists=inspect(c).has_table(table.name,schema='public')
   if exists:c.exec_driver_sql('LOCK TABLE public.'+table.name+' IN SHARE MODE NOWAIT')
   visible=c.execute(text("SELECT pg_catalog.to_regclass(:name)::oid,pg_catalog.to_regclass(:qualified)::oid"),{'name':table.name,'qualified':'public.'+table.name}).one()
   if visible[0]!=visible[1]:raise MigrationError('Membership relation shadowed')
   if exists and c.execute(text("SELECT relkind FROM pg_catalog.pg_class WHERE oid=pg_catalog.to_regclass(:name)"),{'name':'public.'+table.name}).scalar_one()!='r':raise MigrationError('Membership table differs')
def _footprint(c):
 tables=[inspect(c).has_table(t.name) for t in TABLES]
 if c.dialect.name=='sqlite':extra=sum(c.execute(text("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name=:name"),{'name':n}).scalar_one() for n in sqlite_triggers())
 else:extra=sum(c.execute(text("SELECT count(*) FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.proname=:name"),{'name':n}).scalar_one() for n in pg_functions())
 return any(tables) or bool(extra)
def validate(c):
 _source();_boundary(c);_previous.validate(c)
 for table in TABLES:
  if not inspect(c).has_table(table.name):raise MigrationError('Membership table missing')
  _previous._table(c,table)
 if c.dialect.name=='sqlite':
  actual={n:c.execute(text("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=:name"),{'name':n}).scalar_one_or_none() for n in sqlite_triggers()}
  if any(v is None for v in actual.values()):raise MigrationError('Membership trigger missing')
  if {k:_normal(v) for k,v in actual.items()}!={k:_normal(v) for k,v in sqlite_triggers().items()}:raise MigrationError('Membership trigger differs')
 else:
  for name,body in pg_functions().items():
   rows=c.execute(text("""SELECT p.prosrc,p.prosecdef,p.provolatile,p.proparallel,p.proisstrict,p.proleakproof,p.proconfig,p.prorettype::regtype::text,p.pronargs,l.lanname FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace JOIN pg_catalog.pg_language l ON l.oid=p.prolang WHERE n.nspname='public' AND p.proname=:name"""),{'name':name}).all()
   if rows!=[(body,False,'v','u',False,False,['search_path=pg_catalog, public'],'trigger',0,'plpgsql')]:raise MigrationError('Membership function differs')
  for table in TABLES+(_previous._contract.intent,_previous._contract.objects,):
   name=table.name+'_membership_guard'
   rows=c.execute(text("""SELECT t.tgenabled,pg_catalog.pg_get_triggerdef(t.oid),n.nspname,p.proname FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_proc p ON p.oid=t.tgfoid JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace WHERE t.tgrelid=pg_catalog.to_regclass(:table) AND t.tgname=:name"""),{'table':'public.'+table.name,'name':name}).all()
   # pg_get_triggerdef emits canonical event ordering: INSERT OR DELETE OR UPDATE.
   expected=pg_triggers()[name].replace('INSERT OR UPDATE OR DELETE','INSERT OR DELETE OR UPDATE').replace('EXECUTE FUNCTION public.','EXECUTE FUNCTION ')
   if len(rows)!=1 or rows[0][0]!='O' or _normal(rows[0][1])!=_normal(expected) or rows[0][2:]!=('public',name):raise MigrationError('Membership native trigger differs')
 validate_retained(c)
def apply(c):
 _source();_boundary(c);_previous.validate(c)
 if _footprint(c):validate(c);return
 for table in TABLES:table.create(c)
 if c.dialect.name=='sqlite':
  for sql in sqlite_triggers().values():c.exec_driver_sql(sql)
 else:
  for name,body in pg_functions().items():c.exec_driver_sql('CREATE FUNCTION public.'+name+'() RETURNS trigger LANGUAGE plpgsql SET search_path TO pg_catalog, public AS $membership$'+body+'$membership$')
  for sql in pg_triggers().values():c.exec_driver_sql(sql)
 validate(c)
