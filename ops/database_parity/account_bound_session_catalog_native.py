"""Catalog qualification from reviewed source DDL only; not native parity acceptance."""
import os,stat,secrets,tempfile,json,logging
from pathlib import Path
private=Path(os.environ['CALLBACK_NATIVE_PASSWORD_FILE'])
assert os.getuid()==10001 and stat.S_IMODE(private.stat().st_mode)==0o600
print('runtime-private-file uid=10001 mode=600',flush=True)
owned=tempfile.TemporaryDirectory(prefix='owned-account-catalog-')
os.environ['ODIN_DATA_DIR']=owned.name;os.environ['JWT_SECRET_KEY']=secrets.token_urlsafe(48)
os.environ['DATABASE_URL']='postgresql+psycopg://postgres@pg/postgres';os.environ['DATABASE_PASSWORD_FILE']=str(private)
logging.getLogger('httpx').disabled=True;logging.getLogger('httpcore').disabled=True
from sqlalchemy import create_engine,text
from sqlalchemy.engine import URL
from core.schema import bootstrap_database
from core import account_lifetime_schema as s
engine=create_engine(URL.create('postgresql+psycopg',username='postgres',password=private.read_text(),host='pg',database='postgres'),hide_parameters=True)
bootstrap_database(engine,Path('/source/backend'))

from core import account_bound_session_schema as session
with engine.connect() as c:
 tx=c.begin();c.exec_driver_sql('SELECT 1');s.install(c)
 def catalog():
  return {
   'columns':s.rows(c,"SELECT column_name,data_type,character_maximum_length,is_nullable,column_default FROM information_schema.columns WHERE table_schema='public' AND table_name='active_sessions' ORDER BY ordinal_position"),
   'constraints':s.rows(c,"SELECT conname,contype,pg_get_constraintdef(oid),convalidated,condeferrable,condeferred FROM pg_constraint WHERE conrelid='public.active_sessions'::regclass ORDER BY conname"),
   'indexes':s.rows(c,"SELECT r.relname,pg_get_indexdef(i.indexrelid),i.indisvalid,i.indisready,i.indimmediate FROM pg_index i JOIN pg_class r ON r.oid=i.indexrelid WHERE i.indrelid='public.active_sessions'::regclass ORDER BY r.relname")}
 original=catalog()
 # Execute only reviewed owned source DDL, not candidate-derived definitions.
 session.execute(c,'ALTER TABLE active_sessions ADD COLUMN '+session.column(True))
 session.execute(c,session.pg_function()[1])
 for ddl in session.pg_triggers().values():session.execute(c,ddl)
 bound=catalog()
 guards=s.rows(c,"SELECT t.tgname,pg_get_triggerdef(t.oid),t.tgenabled,p.proname,n.nspname FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_namespace n ON n.oid=p.pronamespace WHERE NOT t.tgisinternal AND t.tgrelid='public.active_sessions'::regclass ORDER BY t.tgname")
 assert len(guards)==2
 result={'scope':'SOURCE-DDL session catalog diagnostic only; no parity acceptance','original':original,'bound':bound,'triggers':guards}
 tx.rollback();assert not c.exec_driver_sql("SELECT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='active_sessions' AND column_name='account_nonce')").scalar_one()
 result['source_ddl_rollback_verified']=True
 print('ACCOUNT_CATALOG_JSON='+json.dumps(result,sort_keys=True),flush=True)
engine.dispose();owned.cleanup()
print('account-bound-session-catalog-diagnostic-complete',flush=True)
