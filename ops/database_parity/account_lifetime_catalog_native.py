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
with engine.connect() as c:
 tx=c.begin();c.exec_driver_sql('SELECT 1')
 s.install(c)
 diagnostic_refusal='Source-qualified validator accepted exact owned DDL'
 result={'validator_refusal':diagnostic_refusal,'scope':'Source-authored DDL catalog diagnostic only; unqualified validator refusal expected; transaction rolled back','checks':{},'columns':{},'constraints':{},'indexes':{},'functions':{},'triggers':{},'oid_references':{}}
 for table in (*s.TABLES,'users'):
  result['columns'][table]=[dict(r) for r in c.execute(text("SELECT column_name,data_type,is_nullable,column_default,collation_name FROM information_schema.columns WHERE table_schema='public' AND table_name=:t ORDER BY ordinal_position"),{'t':table}).mappings()]
  result['constraints'][table]=[dict(r) for r in c.execute(text("SELECT conname,contype,pg_get_constraintdef(oid) AS definition,convalidated,condeferrable,condeferred,pg_get_expr(conbin,conrelid) AS expression FROM pg_constraint WHERE conrelid=to_regclass(:t) ORDER BY conname"),{'t':'public.'+table}).mappings()]
  result['indexes'][table]=[dict(r) for r in c.execute(text("SELECT r.relname AS indexname,pg_get_indexdef(i.indexrelid) AS indexdef,i.indisvalid,i.indisready,i.indimmediate FROM pg_index i JOIN pg_class r ON r.oid=i.indexrelid WHERE i.indrelid=to_regclass(:t) ORDER BY r.relname"),{'t':'public.'+table}).mappings()]
  result['triggers'][table]=[dict(r) for r in c.execute(text("SELECT t.tgname,pg_get_triggerdef(t.oid) AS definition,t.tgenabled,t.tgtype,p.proname,n.nspname AS function_schema FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_namespace n ON n.oid=p.pronamespace WHERE NOT t.tgisinternal AND t.tgrelid=to_regclass(:t) AND t.tgname LIKE 'account_028_%' ORDER BY t.tgname"),{'t':'public.'+table}).mappings()]
  result['oid_references'][table]=[dict(r) for r in c.execute(text("SELECT x.conname,n.nspname AS target_schema,r.relname AS target_table,x.confkey,x.conkey FROM pg_constraint x JOIN pg_class r ON r.oid=x.confrelid JOIN pg_namespace n ON n.oid=r.relnamespace WHERE x.contype='f' AND x.conrelid=to_regclass(:t) ORDER BY x.conname"),{'t':'public.'+table}).mappings()]
 result['functions']=[dict(r) for r in c.execute(text("SELECT p.proname,p.prosrc,p.prosecdef,p.proconfig,p.pronargs,p.provolatile,p.proparallel,p.procost,p.prorows,p.prosupport,p.pronargdefaults,pg_get_expr(p.proargdefaults,0) AS default_arguments,p.prorettype='trigger'::regtype AS returns_trigger,n.nspname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.proname LIKE 'account_028_%' ORDER BY p.proname")).mappings()]
 result['checks']={r['conname']:r['expression'] for table in s.TABLES for r in result['constraints'][table] if r['contype']=='c'}
 assert len(result['functions'])==6 and sum(len(x) for x in result['triggers'].values())==12
 assert len(result['checks'])==23,len(result['checks'])
 tx.rollback()
 assert not c.exec_driver_sql("SELECT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='users' AND column_name='account_nonce')").scalar_one()
 result['source_ddl_rollback_verified']=True
 print('ACCOUNT_CATALOG_JSON='+json.dumps(result,sort_keys=True),flush=True)
engine.dispose();owned.cleanup()
print('account-lifetime-catalog-diagnostic-complete',flush=True)
