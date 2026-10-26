"""Durable marker gate, real filesystem and actual pooled-connection ownership."""
import json
import os
from pathlib import Path
from datetime import datetime,timezone
from types import SimpleNamespace
import pytest
from sqlalchemy import event,create_engine
from sqlalchemy.pool import QueuePool
from core.postgres_restore_gate import assert_postgres_restore_ready,RestoreConsumerBlocked,MARKER


def normal():
    return dict(schema_version=1,dialect='postgresql',state='staged',sha256='a'*64,size_bytes=100,actor_id=None,staged_at=datetime.now(timezone.utc).isoformat())


def marker(root,value=None):
    path=root/MARKER;path.write_text(json.dumps(normal() if value is None else value));path.chmod(0o600);return path


def test_absent_and_actual_generated_staged_manifest_are_allowed(tmp_path,monkeypatch):
    assert_postgres_restore_ready(tmp_path)
    # Execute actual stage writer, replacing only archive/DB validation effects.
    from modules.system import postgres_backup_service as backup
    candidate=tmp_path/'candidate.dump';candidate.write_bytes(b'x'*100)
    metadata={'sha256':backup._sha256(candidate),'size_bytes':100}
    monkeypatch.setattr(backup,'validate_postgres_restore',lambda *a,**kw:metadata)
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def execute(self,*args):pass
    monkeypatch.setattr(backup,'create_database_engine',lambda *a,**kw:SimpleNamespace(begin=Connection,dispose=lambda:None))
    backup.stage_postgres_restore(candidate,'synthetic-target','synthetic-maintenance',base_dir=tmp_path)
    generated=tmp_path/MARKER;original=generated.read_bytes()
    assert set(json.loads(original))==set(normal())
    assert_postgres_restore_ready(tmp_path)
    assert generated.read_bytes()==original


@pytest.mark.parametrize('change',[
    {'state':'restore_in_progress'}, {'state':'done'}, {'schema_version':True}, {'dialect':'sqlite'},
    {'size_bytes':True}, {'size_bytes':101*1024*1024}, {'sha256':'A'*64}, {'actor_id':True},
    {'staged_at':'2026-10-12T12:00:00'}, {'extra':None}, {'recovery_incomplete':True},
])
def test_unsupported_or_malformed_marker_refuses(tmp_path,change):
    value=normal();value.update(change);path=marker(tmp_path,value);original=path.read_bytes()
    with pytest.raises(RestoreConsumerBlocked):assert_postgres_restore_ready(tmp_path)
    assert path.read_bytes()==original


@pytest.mark.parametrize('raw',[b'{"state":"staged","state":"staged"}',b'\xff',b'[]',b'{"number":NaN}',b'{'*8193,b''])
def test_bounded_strict_json_reader(tmp_path,raw):
    path=tmp_path/MARKER;path.write_bytes(raw);path.chmod(0o600)
    with pytest.raises(RestoreConsumerBlocked):assert_postgres_restore_ready(tmp_path)


@pytest.mark.parametrize('kind',['symlink','hardlink','directory','mode','unsafe_root','missing_root'])
def test_unsafe_marker_or_configured_directory_refuses(tmp_path,kind):
    path=tmp_path/MARKER
    if kind=='missing_root':
        with pytest.raises(RestoreConsumerBlocked):assert_postgres_restore_ready(tmp_path/'missing')
        return
    if kind=='symlink':
        target=tmp_path/'source';target.write_text(json.dumps(normal()));target.chmod(0o600);path.symlink_to(target)
    elif kind=='hardlink':
        target=marker(tmp_path);os.link(target,tmp_path/'alias')
    elif kind=='directory':path.mkdir()
    elif kind=='mode':marker(tmp_path).chmod(0o644)
    elif kind=='unsafe_root':tmp_path.chmod(0o777)
    with pytest.raises(RestoreConsumerBlocked):assert_postgres_restore_ready(tmp_path)


def test_marker_replacement_during_read_and_close_uncertainty_refuse(tmp_path,monkeypatch):
    from core import postgres_restore_gate as gate
    path=marker(tmp_path);original=os.read;replaced=[]
    def read(fd,count):
        data=original(fd,count)
        if data and not replaced:
            replacement=tmp_path/'replacement';replacement.write_bytes(path.read_bytes());replacement.chmod(0o600);replacement.replace(path);replaced.append(True)
        return data
    monkeypatch.setattr(gate.os,'read',read)
    with pytest.raises(RestoreConsumerBlocked):gate.assert_postgres_restore_ready(tmp_path)
    monkeypatch.setattr(gate.os,'read',original)
    actual_close=os.close;attempts=[]
    def close(fd):
        attempts.append(fd);actual_close(fd)
        if len(attempts)==1:raise OSError('Unknown close acknowledgment')
    monkeypatch.setattr(gate.os,'close',close)
    with pytest.raises(RestoreConsumerBlocked):gate.assert_postgres_restore_ready(tmp_path)
    assert len(attempts)==len(set(attempts))


def test_real_sqlalchemy_checkout_refusal_invalidates_without_reconnect(tmp_path,monkeypatch):
    monkeypatch.setenv('ODIN_DATA_DIR',str(tmp_path))
    engine=create_engine('sqlite://',poolclass=QueuePool)
    opened=[];closed=[]
    event.listen(engine,'connect',lambda *args:opened.append(True))
    event.listen(engine,'close',lambda *args:closed.append(True))
    event.listen(engine,'checkout',lambda *args:assert_postgres_restore_ready())
    with engine.connect() as connection:connection.exec_driver_sql('SELECT 1')
    value=normal();value['state']='restore_in_progress';marker(tmp_path,value)
    with pytest.raises(RestoreConsumerBlocked):engine.connect()
    assert len(opened)==1 and len(closed)==1
    engine.dispose()


@pytest.mark.parametrize("unpooled", [False, True])
def test_ordinary_engine_event_order_and_maintenance_exception(tmp_path,monkeypatch,unpooled):
    from core import database_config as config
    from sqlalchemy.engine import make_url
    from sqlalchemy.pool import NullPool
    pool_options={'poolclass':NullPool} if unpooled else {}
    callbacks={};calls=[];options=[]
    monkeypatch.setattr(config,'resolve_database_url',lambda *a,**kw:make_url('postgresql+psycopg://synthetic@db/owned'))
    monkeypatch.setattr(config,'create_engine',lambda *a,**kw:(options.append(kw) or SimpleNamespace()))
    monkeypatch.setattr(config,'assert_postgres_restore_ready',lambda *args:calls.append('gate'))
    def register(engine,name):
        def decorate(fn):callbacks[name]=fn;return fn
        return decorate
    monkeypatch.setattr(config.event,'listens_for',register)
    class Cursor:
        def execute(self,statement,*args):calls.append('shared' if 'pg_advisory' in statement else statement)
        def close(self):calls.append('close')
    connection=SimpleNamespace(cursor=Cursor,commit=lambda:calls.append('commit'))
    config.create_database_engine('synthetic',role='api',**pool_options);callbacks['connect'](connection,SimpleNamespace(invalidate=lambda *args:None));callbacks['checkout'](None,None,None)
    assert calls==['gate',"SET lock_timeout = '5s'",'shared','SET lock_timeout = 0','commit','gate','close','gate']
    assert options[0]['connect_args']=={'application_name':'odin-api','options':'-c TimeZone=UTC'}
    if unpooled:
        assert options[0]['poolclass'] is NullPool
        assert not {'pool_size','max_overflow','pool_timeout','pool_recycle'} & options[0].keys()
    for role in ('backup','restore'):
        calls.clear();callbacks.clear();engine=config.create_database_engine('synthetic',role=role)
        assert not calls and not callbacks and not engine._odin_restore_barrier_required


def test_absent_marker_never_adopts_replaced_data_directory(tmp_path,monkeypatch):
    from core import postgres_restore_gate as gate
    root=tmp_path/'data';root.mkdir();original=os.stat;replaced=[]
    def inspect(path,*args,**kwargs):
        try:return original(path,*args,**kwargs)
        except FileNotFoundError:
            if path==MARKER and not replaced:
                root.rename(tmp_path/'original-data');root.mkdir();replaced.append(True)
            raise
    monkeypatch.setattr(gate.os,'stat',inspect)
    with pytest.raises(RestoreConsumerBlocked):gate.assert_postgres_restore_ready(root)
    assert (tmp_path/'original-data').is_dir() and root.is_dir()


def test_configured_data_directory_symlink_and_relative_configuration_refuse(tmp_path):
    linked=tmp_path/'linked';linked.symlink_to(tmp_path,target_is_directory=True)
    for configured in (linked,'relative-data',''):
        with pytest.raises(RestoreConsumerBlocked):assert_postgres_restore_ready(configured)


def test_sqlite_factory_does_not_depend_on_postgres_marker(tmp_path,monkeypatch):
    from core.database_config import create_database_engine
    monkeypatch.setenv('ODIN_DATA_DIR',str(tmp_path/'does-not-exist'))
    engine=create_database_engine('sqlite://')
    with engine.connect() as db:assert db.exec_driver_sql('SELECT 1').scalar_one()==1
    engine.dispose()


@pytest.mark.parametrize('cleanup_fault',[None,'cursor','connection'])
def test_connect_refusal_closes_exact_owner_once_without_masking_typed_error(tmp_path,monkeypatch,cleanup_fault):
    from core import database_config as config
    from sqlalchemy.engine import make_url
    callbacks={};calls=[];checks=[]
    monkeypatch.setattr(config,'resolve_database_url',lambda *a,**kw:make_url('postgresql+psycopg://synthetic@db/owned'))
    monkeypatch.setattr(config,'create_engine',lambda *a,**kw:SimpleNamespace())
    blocked=RestoreConsumerBlocked('Synthetic durable refusal')
    def gate(*args):
        checks.append(True)
        if len(checks)==2:raise blocked
    monkeypatch.setattr(config,'assert_postgres_restore_ready',gate)
    def register(engine,name):
        def decorate(fn):callbacks[name]=fn;return fn
        return decorate
    monkeypatch.setattr(config.event,'listens_for',register)
    class Cursor:
        def execute(self,statement,*args):calls.append(statement)
        def close(self):
            calls.append('cursor-close')
            if cleanup_fault=='cursor':raise OSError('Synthetic cursor cleanup uncertainty')
    class Connection:
        def cursor(self):return Cursor()
        def commit(self):calls.append('commit')
        def close(self):
            calls.append('connection-close')
            if cleanup_fault=='connection':raise OSError('Synthetic connection cleanup uncertainty')
    config.create_database_engine('synthetic',role='api')
    with pytest.raises(RestoreConsumerBlocked) as result:callbacks['connect'](Connection(),None)
    assert result.value is blocked
    assert calls[-2:]==['cursor-close','connection-close'] and calls.count('connection-close')==1
    assert all('SELECT' not in call or 'pg_advisory' in call for call in calls)
    assert getattr(blocked,'cleanup_uncertain',False)==(cleanup_fault is not None)


@pytest.mark.parametrize('url',['postgresql://synthetic@db/owned','postgres://synthetic@db/owned','postgresql+psycopg://synthetic@db/owned'])
def test_waiter_all_postgres_url_forms_refuse_before_engine_creation(tmp_path,monkeypatch,url):
    from scripts import wait_for_database_schema as waiter
    value=normal();value.update(state='restore_in_progress',rollback_backup='rollback_20261013_000000_000000.dump',
        rollback_sha256='b'*64,rollback_size_bytes=100,apply_started_at=value['staged_at'])
    marker(tmp_path,value)
    monkeypatch.setenv('ODIN_DATA_DIR',str(tmp_path));monkeypatch.setenv('DATABASE_URL',url)
    calls=[]
    def engine(*args,**kwargs):calls.append(True);raise AssertionError('Engine created before explicit waiter refusal')
    monkeypatch.setattr(waiter,'create_database_engine',engine)
    with pytest.raises(RestoreConsumerBlocked):waiter.main()
    assert calls==[]


def test_waiter_sqlite_path_remains_usable_with_missing_postgres_data_dir(tmp_path,monkeypatch):
    from scripts import wait_for_database_schema as waiter
    from sqlalchemy import create_engine
    path=tmp_path/'schema.sqlite';engine=create_engine('sqlite:///'+str(path))
    with engine.begin() as db:db.exec_driver_sql('CREATE TABLE odin_schema_migrations (id INTEGER PRIMARY KEY)')
    engine.dispose()
    monkeypatch.setenv('DATABASE_URL','sqlite:///'+str(path));monkeypatch.setenv('ODIN_DATA_DIR',str(tmp_path/'missing'))
    monkeypatch.setattr(waiter,'assert_postgres_restore_ready',lambda:(_ for _ in ()).throw(AssertionError('SQLite entered PostgreSQL gate')))
    monkeypatch.setattr(waiter,'import_all_models',lambda:None)
    monkeypatch.setattr(waiter,'validate_schema',lambda connection:None)
    assert waiter.main()==0


def test_waiter_url_parse_failure_is_sanitized_without_parser_context(monkeypatch):
    from scripts import wait_for_database_schema as waiter
    from core.database_config import DatabaseConfigurationError
    monkeypatch.setenv('DATABASE_URL','postgresql://synthetic@db:invalid/owned')
    with pytest.raises(DatabaseConfigurationError,match='DATABASE_URL is invalid') as result:waiter.main()
    assert result.value.__suppress_context__ and str(result.value)=='DATABASE_URL is invalid'
