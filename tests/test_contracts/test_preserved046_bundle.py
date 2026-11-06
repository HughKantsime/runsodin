"""Keep the released 046 schema restorable after adding traceability."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import pytest
from core.schema import preserved046


def test_isolated046_boot_repeat_upgrade_and_ledger(tmp_path):
    code = '''
import runpy,sys
namespace=runpy.run_path(sys.argv[1],run_name='frozen046')
bundle,manifest=namespace['prepare_sources']()
from sqlalchemy import create_engine,inspect,text
from core.schema import bootstrap_database
from core.base import AlertType
assert 'print_paused' in [t.value for t in AlertType]
engine=create_engine('sqlite:///'+sys.argv[2])
assert 'python:046-tray-identity' in bootstrap_database(engine)['applied']
assert 'tray_observation' in {c['name'] for c in inspect(engine).get_columns('filament_slots')}
assert 'traceability_records' not in inspect(engine).get_table_names()
assert bootstrap_database(engine)['applied']==[]
with engine.connect() as c:
 assert {r[0] for r in c.execute(text('SELECT migration_id FROM odin_schema_migrations'))}==set(manifest['ledger_ids'])
'''
    runner = Path(preserved046.__file__).with_name('runner.py')
    result = subprocess.run([sys.executable, '-I', '-c', code, str(runner), str(tmp_path/'frozen.db')],
                            capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    from sqlalchemy import create_engine
    from core.schema import bootstrap_database
    engine = create_engine('sqlite:///' + str(tmp_path/'frozen.db'))
    try:
        from core.schema.bootstrap import POST027_MIGRATIONS
        assert bootstrap_database(engine)['applied'] == [
            migration_id for migration_id, _ in POST027_MIGRATIONS
            if migration_id not in preserved046.ledger_ids()
        ]
        assert bootstrap_database(engine)['applied'] == []
    finally:
        engine.dispose()


@pytest.mark.parametrize('version', ['2.0.42', '2.0.43', '2.0.44'])
def test_exact_released_046_ledgers_and_restore_routing(tmp_path, monkeypatch, version):
    from core.schema import preserved029, preserved045
    from modules.system import pre026_restore_stage as stage, postgres_backup_service as backup
    ids = preserved046.ledger_ids()
    assert ids == preserved045.ledger_ids() | {'python:046-tray-identity'}
    releases = json.loads(Path('backend/core/schema/released_post027_ledgers.json').read_text())['releases']
    assert ids == preserved029.ledger_ids() | set(releases[version])
    assert 'python:052-traceability' not in ids
    archive = tmp_path/'owned.dump'; archive.write_bytes(b'x'*100)
    monkeypatch.setattr(backup, '_run_pg_tool', lambda *a: '')
    monkeypatch.setattr(backup, '_toc_inventory', lambda *a: None)
    monkeypatch.setattr(stage, 'archive_ledger_ids', lambda *a: ids)
    assert stage.identify_contract(archive, None, None, tmp_path) is stage.PRESERVED046


@pytest.mark.parametrize('changed', ['manifest.json', 'overlay/core/schema/migrations/046_tray_identity.py'])
def test_tamper_refused(tmp_path, changed):
    from core.schema.preserved046.integrity import check_package, Preserved046Error
    base = Path(preserved046.__file__).parent
    target = tmp_path/'preserved046'
    shutil.copytree(base, target, ignore=shutil.ignore_patterns('__pycache__'))
    (tmp_path/'preserved045').symlink_to(base.parent/'preserved045', target_is_directory=True)
    with (target/changed).open('ab') as f:
        f.write(b'\n# changed\n')
    with pytest.raises(Preserved046Error, match='differs'):
        check_package(target)


@pytest.mark.parametrize('message,archive_refusal', [
    ('Preserved046 stage ledger differs: owned fixture', True),
    ('Preserved046 package hash differs', False),
])
def test_restore_classifies_preserved046_stage_errors(tmp_path,monkeypatch,message,archive_refusal):
    from unittest.mock import MagicMock
    from modules.system import pre026_restore_stage as stage, postgres_backup_service as backup, restore_artifacts
    from core import database_config
    archive=tmp_path/'owned.dump';archive.write_bytes(b'owned fixture')
    monkeypatch.setattr(stage,'identify_contract',lambda *a:stage.PRESERVED046)
    monkeypatch.setattr(backup,'_require_distinct_maintenance_secret',lambda *a:None)
    monkeypatch.setattr(backup,'_maintenance_urls',lambda *a:('unused','unused'))
    engine=MagicMock();engine.execution_options.return_value=engine
    monkeypatch.setattr(database_config,'create_database_engine',lambda *a,**k:engine)
    for name in ('sweep_stale','record','forget','drop_database'):
        monkeypatch.setattr(restore_artifacts,name,lambda *a,**k:None)
    def refuse(*a,**k): raise preserved046.Preserved046Error(message)
    monkeypatch.setattr(preserved046,'restore_into_stage',refuse)
    expected=backup.ArchiveRefused if archive_refusal else backup.RestoreEnvironmentError
    with pytest.raises(expected,match=message):
        with stage.prepared_archive(archive,'postgresql://localhost/target','postgresql://localhost/maintenance',None,None,tmp_path):
            pytest.fail('Invalid preserved stage must not be yielded')



def test_child_success_era_is_accepted_by_parent():
    import ast
    tree=ast.parse(Path(preserved046.__file__).with_name('runner.py').read_text())
    eras=[node.value.value for node in ast.walk(tree) if isinstance(node,ast.Assign)
          and any(isinstance(target,ast.Name) and target.id=='era' for target in node.targets)
          and isinstance(node.value,ast.Constant)]
    assert eras
    for era in eras:
        assert preserved046._ACK.fullmatch(f'preserved046-validated:{era}\n'.encode())


def test_stale_046_work_and_confirmed_child_journal_are_cleaned(tmp_path):
    import os
    import time
    from modules.system import restore_artifacts as artifacts
    journal=artifacts.journal_dir(tmp_path)
    child=journal/('.odin-preserved046-'+'a'*20);child.mkdir(mode=0o700)
    confirmed='odin_validate_'+'b'*20;intended='odin_validate_'+'c'*20
    (child/confirmed).write_text('');(child/(intended+'.intent')).write_text('')
    work=tmp_path/('.odin-preserved046-'+'d'*20);work.mkdir()
    unrelated=tmp_path/'keep-me';unrelated.mkdir()
    old=time.time()-7*3600
    for path in (child/confirmed,child/(intended+'.intent'),child,work,unrelated):os.utime(path,(old,old))
    asked=[]
    result=artifacts.sweep_stale(tmp_path,'unused',None,drop=lambda names:asked.extend(names) or names)
    assert asked==[confirmed] and result['databases']==[confirmed]
    assert not child.exists() and not work.exists() and unrelated.exists()
