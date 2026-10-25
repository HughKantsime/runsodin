"""Canonical lexical evidence and actual restore use-list safety."""
from pathlib import Path
from types import SimpleNamespace
from contextlib import contextmanager
import hashlib
import json
import pytest
from modules.system.backup_service import BackupValidationError
from modules.system import postgres_backup_service as backup
from modules.system.postgres_canonical_executables import sql_statements, validate_schema_sql, canonical_functions, canonical_triggers
from ._preserved029_target import bundle_only


@pytest.mark.parametrize('original,canonical',[
    ('SELECT  1 ;-- metadata\nSELECT 2;', 'SELECT 2;SELECT 1;'),
    ('SELECT /* outer /* nested */ comment */ 1;', 'SELECT 1;'),
    ("SELECT 'same; -- literal /* nested */';", "SELECT 'same; -- literal /* nested */';"),
    ('SELECT $$same; -- dollar /* body */$$;', 'SELECT $$same; -- dollar /* body */$$;'),
    ('\\restrict abc123\nSELECT 1;\n\\unrestrict abc123\n','SELECT 1;'),
    ("SELECT E'escaped\\\';still string';", "SELECT E'escaped\\\';still string';"),
    ('SELECT "quoted;identifier";','SELECT "quoted;identifier";'),
])
def test_lexical_equal_preserves_literals_nestedcomments_and_controlpair(original,canonical):
    validate_schema_sql(original,canonical)


@pytest.mark.parametrize('original,canonical',[
    ("SELECT 'USD';", "SELECT 'usd';"),
    ("SELECT 'two  spaces';", "SELECT 'two spaces';"),
    ("SELECT 'a\n-- literal';", "SELECT 'a\n';"),
    ('SELECT $$body;write_marker()$$;', 'SELECT $$body;no_marker()$$;'),
    ('SELECT 1;SELECT 1;','SELECT 1;'),
    ('SELECT 1+2;', 'SELECT 1+3;'),
    ('SELECT "Case";','SELECT "case";'),
    ('SELECT 1;SELECT write_marker();','SELECT 1;'),
])
def test_schema_expression_body_literal_duplicate_mutation_refuses(original,canonical):
    with pytest.raises(BackupValidationError,match='differ'):validate_schema_sql(original,canonical)


@pytest.mark.parametrize('sql',[
    "SELECT 'open;", 'SELECT $tag$open;', 'SELECT 1 /* open;',
    '\\restrict a\nSELECT 1;\n\\unrestrict b\n', '\\restrict a\nSELECT 1;',
    '\\restrict a\n\\restrict a\nSELECT 1;', '\\unrestrict a\nSELECT 1;',
    '\\connect attacker\nSELECT 1;', 'SELECT 1;\\! touch marker\n', 'SELECT 1',
])
def test_malformed_and_unknown_control_records_refuse(sql):
    with pytest.raises(BackupValidationError):sql_statements(sql)


def toc():
    rows=[f'{i}; 1259 {i} TABLE public {table} owner' for i,table in enumerate(sorted(backup.canonical_tables()),1)]
    rows+=['10000; 1255 10000 FUNCTION public orders_economic_guard() owner',
           '10001; 2620 10001 TRIGGER public orders orders_economic_guard owner',
           '10002; 0 10002 TABLE DATA public orders owner',
           '10003; 0 10003 SEQUENCE SET public orders_id_seq owner']
    return '\n'.join(rows)


def test_actual_restore_list_excludes_all_schema_and_executable_entries(tmp_path,monkeypatch):
    monkeypatch.setattr(backup,'canonical_tables',lambda:frozenset({'orders'}))
    monkeypatch.setattr(backup,'_trusted_restore_foreign_keys',lambda c:[])
    monkeypatch.setattr(backup,'_run_pg_tool',lambda *a:toc())
    listing=backup._validated_restore_list(tmp_path/'archive.dump','postgresql://owned@pg/owned',None,tmp_path,trusted_prevalidated=True,data_connection=object())
    assert listing.read_text()=='10002; 0 10002 TABLE DATA public orders owner\n10003; 0 10003 SEQUENCE SET public orders_id_seq owner\n'
    assert listing.stat().st_mode&0o777==0o600


@pytest.mark.parametrize('entry',[
    '10000; 1255 10000 FUNCTION public orders_economic_guard(integer) owner',
    '10000; 1255 10000 FUNCTION private orders_economic_guard() owner',
    '10000; 1255 10000 FUNCTION public malicious() owner',
    '10000; 2620 10000 TRIGGER public orders malicious owner',
])
def test_noncanonical_function_overload_trigger_alias_rejected(entry):
    base='\n'.join(toc().splitlines()[:-4])
    with pytest.raises(BackupValidationError):backup.validate_archive_toc(base+'\n'+entry,allow_unresolved_structure=True)


@bundle_only
def test_exact_source_registry_has_expected_union_counts():
    from core.schema.pre026_restore_contract import PRE027_RESTORE026
    functions=canonical_functions(); triggers=canonical_triggers()
    previous_functions=canonical_functions(_contract=PRE027_RESTORE026)
    previous_triggers=canonical_triggers(_contract=PRE027_RESTORE026)
    assert len(previous_functions)==20 and len(previous_triggers)==29
    assert len(functions)==21 and len(triggers)==31
    assert set(functions)-set(previous_functions)=={'power_target_operation_guard'}
    assert set(triggers)-set(previous_triggers)=={
        ('power_target_operations','power_target_operations_row_guard'),
        ('power_target_operations','power_target_operations_no_truncate'),
    }
    assert all(functions[k]==v for k,v in previous_functions.items())
    assert all(triggers[k]==v for k,v in previous_triggers.items())
    assert all(v[6]=='' for v in functions.values())


def test_failed_interrupted_recovery_retains_actual_barrier_objects_and_manifest(tmp_path,monkeypatch):
    target='postgresql://owned@pg/owned'
    paths=backup.postgres_paths(tmp_path);paths.backups.mkdir()
    paths.pending.write_bytes(b'x'*100)
    rollback=paths.backups/'rollback_20261012_120000_123456.dump';rollback.write_bytes(b'y'*100)
    checksum=hashlib.sha256(rollback.read_bytes()).hexdigest()
    manifest={'schema_version':1,'dialect':'postgresql','state':'restore_in_progress',
              'rollback_backup':rollback.name,'rollback_sha256':checksum,'rollback_size_bytes':100}
    paths.manifest.write_text(json.dumps(manifest))
    calls=[]
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def execute(self,sql,*args):
            calls.append(str(sql));return SimpleNamespace(scalar_one=lambda:77)
        def commit(self):calls.append('commit')
        def rollback(self):calls.append('rollback')
        def close(self):calls.append('barrier-close')
    barrier=Connection()
    class Engine:
        def __init__(self,kind):self.kind=kind
        def connect(self):return barrier if self.kind=='barrier' else Connection()
        def dispose(self):calls.append(self.kind+'-dispose')
    engines=[Engine('maintenance'),Engine('barrier')]
    monkeypatch.setattr(backup,'create_database_engine',lambda *a,**kw:engines.pop(0))
    monkeypatch.setattr(backup,'_assert_no_target_sessions',lambda *a,**kw:None)
    monkeypatch.setattr(backup,'inspect_postgres_archive',lambda *a,**kw:{'sha256':checksum,'size_bytes':100})
    def fail(*args):raise RuntimeError('Synthetic rollback failure')
    monkeypatch.setattr(backup,'_restore_postgres_archive',fail)
    try:
        with pytest.raises(BackupValidationError,match='recovery incomplete'):
            backup.apply_pending_postgres_restore(target,'postgresql://owned@pg/postgres',acknowledgement=None,
                password_file='app-file',maintenance_password_file='maintenance-file',base_dir=tmp_path)
        assert not any('pg_advisory_unlock' in call for call in calls)
        assert 'barrier-close' not in calls and 'barrier-dispose' not in calls
        assert backup._FAILED_RESTORE_BARRIERS[target][0] is barrier
        retained=json.loads(paths.manifest.read_text())
        assert retained['state']=='restore_in_progress' and retained['recovery_incomplete'] is True
        assert paths.pending.exists() and rollback.exists()
    finally:backup._FAILED_RESTORE_BARRIERS.pop(target,None)


def test_existing_follower_schema_gate_refuses_unfinished_restore_marker(tmp_path,monkeypatch):
    from scripts import wait_for_database_schema as gate
    marker=tmp_path/'restore-pending-postgres.json'
    marker.write_text('{"state":"restore_in_progress","recovery_incomplete":true}')
    @contextmanager
    def connect():yield object()
    monkeypatch.setenv('DATABASE_URL','postgresql://owned@pg/owned')
    monkeypatch.setenv('ODIN_DATA_DIR',str(tmp_path))
    monkeypatch.setattr(gate,'create_database_engine',lambda *a,**kw:SimpleNamespace(connect=connect,dispose=lambda:None))
    monkeypatch.setattr(gate,'import_all_models',lambda:None)
    monkeypatch.setattr(gate,'inspect',lambda c:SimpleNamespace(get_table_names=lambda:['odin_schema_migrations']))
    monkeypatch.setattr(gate,'validate_schema',lambda c:None)
    from core.postgres_restore_gate import RestoreConsumerBlocked
    with pytest.raises(RestoreConsumerBlocked):gate.main()
    assert marker.exists() # Reproduced gap; do not present as durable process-death protection.


def test_reordered_live_body_with_identical_statement_multiset_is_rejected():
    from modules.system.postgres_canonical_executables import validate_executables
    expected=canonical_functions()
    original=expected['cost_participation_guard'][0]
    changed=original.replace(' RETURN NEW;\n','').replace(' END IF;\n',' END IF;\n RETURN NEW;\n',1)
    assert sql_statements(changed)==sql_statements(original)
    class Connection:
        def execute(self,statement):
            if 'FROM pg_proc' in str(statement):
                rows=[]
                for name,values in expected.items():
                    row=list(values)
                    if name=='cost_participation_guard':row[0]=changed
                    rows.append((name,*row))
            else:rows=[(*key,*values) for key,values in canonical_triggers().items()]
            return SimpleNamespace(all=lambda:rows)
    with pytest.raises(BackupValidationError,match='noncanonical'):
        validate_executables(Connection())


def test_lifetime_registry_uses_exact_frozen_source_bodies_and_triggers():
    import importlib
    migration=importlib.import_module('core.schema.migrations.022_economic_source_lifetimes')
    for name,body in migration._pg_functions().items():
        assert canonical_functions()[name][0]==body
        assert canonical_functions()[name][1]==['search_path=pg_catalog, public']
    for key,definition in migration._pg_triggers().items():
        assert canonical_triggers()[key]==(definition,key[1],'public','O')


def data_line(table,number=1):
    return f'{number}; 0 {number} TABLE DATA public {table} owner'

def test_trusted_data_dag_preserves_toc_and_places_sequences_last():
    lines=[data_line('child',4),'9; 0 9 SEQUENCE SET public child_id_seq owner',data_line('parent',7),data_line('alpha',3)]
    assert backup._ordered_restore_data(lines,{'child','parent','alpha'},[('child','parent',False,False)])==[lines[3],lines[2],lines[0],lines[1]]

@pytest.mark.parametrize('lines,tables,fks',[
    ([data_line('a')],{'a','b'},[]),
    ([data_line('a'),data_line('a',2)],{'a'},[]),
    ([data_line('foreign')],{'a'},[]),
    ([data_line('a')],{'a'},[('a','missing',False,False)]),
    ([data_line('a')],{'a'},[('a','private.a',False,False)]),
    ([data_line('a'),data_line('b')],{'a','b'},[('a','b',True,False),('b','a',False,False)]),
])
def test_trusted_data_dag_refuses_missing_duplicate_foreign_and_immediate_cycles(lines,tables,fks):
    with pytest.raises(BackupValidationError):backup._ordered_restore_data(lines,tables,fks)

def test_trusted_data_dag_ignores_only_self_and_initially_deferred_edges():
    lines=[data_line('a'),data_line('b')]
    assert backup._ordered_restore_data(lines,{'a','b'},[('a','a',False,False),('a','b',True,True),('b','a',False,False)])==lines


@pytest.mark.parametrize('failure',['ordering','writing'])
def test_restore_list_failure_removes_only_owned_temp(tmp_path,monkeypatch,failure):
    original=tmp_path/'preserved.dump';original.write_bytes(b'original')
    unrelated=tmp_path/'.odin-restore-list-unrelated';unrelated.write_text('preserve')
    monkeypatch.setattr(backup,'canonical_tables',lambda:frozenset({'orders'}))
    monkeypatch.setattr(backup,'_trusted_restore_foreign_keys',lambda c:[])
    monkeypatch.setattr(backup,'_run_pg_tool',lambda *a:toc())
    def fail(*args):raise OSError('Injected owned list failure')
    monkeypatch.setattr(backup,'_ordered_restore_data' if failure=='ordering' else 'os',
        fail if failure=='ordering' else SimpleNamespace(fdopen=backup.os.fdopen,fsync=fail))
    with pytest.raises(OSError,match='Injected owned list failure'):
        backup._validated_restore_list(original,'postgresql://owned@pg/owned',None,tmp_path,
            trusted_prevalidated=True,data_connection=object())
    assert sorted(p.name for p in tmp_path.iterdir())==['.odin-restore-list-unrelated','preserved.dump']
    assert original.read_bytes()==b'original' and unrelated.read_text()=='preserve'


def canonical_connection(function_change=None,trigger_change=None):
    class Connection:
        def execute(self,statement):
            if 'FROM pg_proc' in str(statement):
                rows=[(name,*values) for name,values in canonical_functions().items()]
                if function_change:rows=function_change(rows)
            else:
                rows=[(*key,*values) for key,values in canonical_triggers().items()]
                if trigger_change:rows=trigger_change(rows)
            return SimpleNamespace(all=lambda:rows)
    return Connection()

def test_exact_end_actor_body_passes_without_relaxing_attributes_or_archive_lexer():
    from modules.system.postgres_canonical_executables import validate_executables
    from core.audit_actor_schema import PG_BODY,FUNCTION
    assert PG_BODY.endswith('END') and not PG_BODY.endswith(';')
    validate_executables(canonical_connection())
    with pytest.raises(BackupValidationError):sql_statements(PG_BODY)
    for body in (PG_BODY+' ',PG_BODY.replace('immutable','altered')):
        def changed(rows):return [(r[0],body,*r[2:]) if r[0]==FUNCTION else r for r in rows]
        with pytest.raises(BackupValidationError):validate_executables(canonical_connection(function_change=changed))

@pytest.mark.parametrize('config',[None,['search_path=pg_catalog, public'],['search_path=pg_catalog']])
def test_install_exact_supported_source_configuration(monkeypatch,config):
    from modules.system import postgres_canonical_executables as guards
    monkeypatch.setattr(guards,'canonical_functions',lambda:{'owned':('BEGIN RETURN NEW; END;',config)})
    monkeypatch.setattr(guards,'canonical_triggers',lambda:{})
    monkeypatch.setattr(guards,'validate_executables',lambda c:None)
    statements=[]
    guards.install_trusted_guards(SimpleNamespace(exec_driver_sql=statements.append))
    expected='' if config is None else (' SET search_path TO pg_catalog, public' if 'public' in config[0] else ' SET search_path TO pg_catalog')
    assert statements==['CREATE FUNCTION public.owned() RETURNS trigger LANGUAGE plpgsql'+expected+' AS $odin_trusted$BEGIN RETURN NEW; END;$odin_trusted$']

def test_install_unknown_configuration_refuses(monkeypatch):
    from modules.system import postgres_canonical_executables as guards
    monkeypatch.setattr(guards,'canonical_functions',lambda:{'owned':('BEGIN RETURN NEW; END;',['search_path=public'])})
    with pytest.raises(BackupValidationError):guards.install_trusted_guards(SimpleNamespace(exec_driver_sql=lambda s:pytest.fail('Must not execute')))


def test_dispatch_recovery_executables_are_exact_and_tampering_is_rejected():
    import importlib
    from modules.system.postgres_canonical_executables import validate_executables
    recovery = importlib.import_module('core.schema.migrations.038_dispatch_recovery')
    functions, triggers = canonical_functions(), canonical_triggers()
    assert functions['odin_recovery_immutable'] == (
        recovery.pg_functions()['odin_recovery_immutable'], None, False, 'v', 'u',
        'trigger', '', 'plpgsql', False, False, 'f', False, 0, 100.0, 0.0, '-')
    for key, definition in recovery.pg_triggers().items():
        assert triggers[key] == (definition, 'odin_recovery_immutable', 'public', 'O')

    class Connection:
        def __init__(self, changed=False):
            self.changed = changed
        def execute(self, query):
            if 'FROM pg_proc' in str(query):
                rows = [(name, *values) for name, values in functions.items()]
                if self.changed:
                    rows = [(name, source.replace('immutable recovery history', 'tampered'), *rest)
                            for name, source, *rest in rows]
            else:
                rows = [(table, name, *values) for (table, name), values in triggers.items()]
            return SimpleNamespace(all=lambda: rows)
    validate_executables(Connection())
    with pytest.raises(BackupValidationError, match='noncanonical'):
        validate_executables(Connection(changed=True))
