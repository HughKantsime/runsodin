"""2.0.26's exact 043 schema remains restorable without current 043 sources."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import pytest
from core.schema import preserved043


def test_isolated043_boot_repeat_and_ledger(tmp_path):
    runner=Path(preserved043.__file__).with_name('runner.py')
    code='''
import runpy,sys
namespace=runpy.run_path(sys.argv[1],run_name='frozen043')
bundle,manifest=namespace['prepare_sources']()
from sqlalchemy import create_engine,inspect,text
from core.schema import bootstrap_database
engine=create_engine('sqlite:///'+sys.argv[2])
assert 'python:043-recipe-configuration' in bootstrap_database(engine)['applied']
assert 'terminal_filament_measurements' in inspect(engine).get_table_names()
assert 'job_summary_outbox' in inspect(engine).get_table_names()
assert 'color_name' in {c['name'] for c in inspect(engine).get_columns('spools')}
assert 'tray_uuid' not in {c['name'] for c in inspect(engine).get_columns('spools')}
assert bootstrap_database(engine)['applied']==[]
with engine.connect() as c:
 assert {r[0] for r in c.execute(text('SELECT migration_id FROM odin_schema_migrations'))}==set(manifest['ledger_ids'])
'''
    result=subprocess.run([sys.executable,'-I','-c',code,str(runner),str(tmp_path/'frozen.db')],capture_output=True,text=True,timeout=90)
    assert result.returncode==0,result.stderr
    # A staged historical database must upgrade through the live additive path.
    from sqlalchemy import create_engine, inspect
    from core.schema import bootstrap_database
    engine = create_engine('sqlite:///' + str(tmp_path/'frozen.db'))
    try:
        assert bootstrap_database(engine)['applied'] == ['python:044-spool-tray-uuid', 'python:045-pause-cancel-alerts']
        assert 'tray_uuid' in {c['name'] for c in inspect(engine).get_columns('spools')}
        assert bootstrap_database(engine)['applied'] == []
    finally:
        engine.dispose()


@pytest.mark.parametrize('version', ['2.0.24', '2.0.25', '2.0.26'])
def test_exact_released_043_ledger(version):
    from core.schema import preserved040
    assert preserved043.ledger_ids()==preserved040.ledger_ids()|{'python:041-inventory-color', 'python:042-dispatch-capability', 'python:043-recipe-configuration'}
    released=json.loads(Path('backend/core/schema/released_post027_ledgers.json').read_text())['releases'][version]
    assert set(released)<=preserved043.ledger_ids()
    assert 'python:044-spool-tray-uuid' not in preserved043.ledger_ids()


@pytest.mark.parametrize('changed',['manifest.json','overlay/core/schema/migrations/043_recipe_configuration.py'])
def test_tamper_refused(tmp_path,changed):
    from core.schema.preserved043.integrity import check_package,Preserved043Error
    base=Path(preserved043.__file__).parent;target=tmp_path/'preserved043'
    shutil.copytree(base,target,ignore=shutil.ignore_patterns('__pycache__'))
    (tmp_path/'preserved040').symlink_to(base.parent/'preserved040',target_is_directory=True)
    with (target/changed).open('ab') as file:file.write(b'\n# changed\n')
    with pytest.raises(Preserved043Error,match='differs'):check_package(target)


@pytest.mark.parametrize('message,archive_refusal', [
    ('Preserved043 stage ledger differs: owned fixture', True),
    ('Preserved043 package hash differs', False),
])
def test_restore_classifies_preserved043_stage_errors(tmp_path,monkeypatch,message,archive_refusal):
    from unittest.mock import MagicMock
    from modules.system import pre026_restore_stage as stage, postgres_backup_service as backup, restore_artifacts
    from core import database_config
    archive=tmp_path/'owned.dump';archive.write_bytes(b'owned fixture')
    monkeypatch.setattr(stage,'identify_contract',lambda *a:stage.PRESERVED043)
    monkeypatch.setattr(backup,'_require_distinct_maintenance_secret',lambda *a:None)
    monkeypatch.setattr(backup,'_maintenance_urls',lambda *a:('unused','unused'))
    engine=MagicMock();engine.execution_options.return_value=engine
    monkeypatch.setattr(database_config,'create_database_engine',lambda *a,**k:engine)
    for name in ('sweep_stale','record','forget','drop_database'):
        monkeypatch.setattr(restore_artifacts,name,lambda *a,**k:None)
    def refuse(*a,**k): raise preserved043.Preserved043Error(message)
    monkeypatch.setattr(preserved043,'restore_into_stage',refuse)
    expected=backup.ArchiveRefused if archive_refusal else backup.RestoreEnvironmentError
    with pytest.raises(expected,match=message):
        with stage.prepared_archive(archive,'postgresql://localhost/target','postgresql://localhost/maintenance',None,None,tmp_path):
            pytest.fail('Invalid preserved stage must not be yielded')



def test_child_success_era_is_accepted_by_parent():
    import ast
    tree=ast.parse(Path(preserved043.__file__).with_name('runner.py').read_text())
    eras=[node.value.value for node in ast.walk(tree) if isinstance(node,ast.Assign)
          and any(isinstance(target,ast.Name) and target.id=='era' for target in node.targets)
          and isinstance(node.value,ast.Constant)]
    assert eras
    for era in eras:
        assert preserved043._ACK.fullmatch(f'preserved043-validated:{era}\n'.encode())


def test_stale_043_work_and_confirmed_child_journal_are_cleaned(tmp_path):
    import os
    import time
    from modules.system import restore_artifacts as artifacts
    journal=artifacts.journal_dir(tmp_path)
    child=journal/('.odin-preserved043-'+'a'*20);child.mkdir(mode=0o700)
    confirmed='odin_validate_'+'b'*20;intended='odin_validate_'+'c'*20
    (child/confirmed).write_text('');(child/(intended+'.intent')).write_text('')
    work=tmp_path/('.odin-preserved043-'+'d'*20);work.mkdir()
    unrelated=tmp_path/'keep-me';unrelated.mkdir()
    old=time.time()-7*3600
    for path in (child/confirmed,child/(intended+'.intent'),child,work,unrelated):os.utime(path,(old,old))
    asked=[]
    result=artifacts.sweep_stale(tmp_path,'unused',None,drop=lambda names:asked.extend(names) or names)
    assert asked==[confirmed] and result['databases']==[confirmed]
    assert not child.exists() and not work.exists() and unrelated.exists()
