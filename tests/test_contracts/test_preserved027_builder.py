"""Closed source package/isolated import admission. Real PG follows review."""
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from time import monotonic
import pytest
from core.schema import preserved027 as p
from core.schema.preserved027 import integrity

BASE=Path(p.__file__).parent


def test_exact_accepted_source_bytes_and_complete_closed_inventory():
    bundle,manifest=integrity.check_package()
    from core.schema.prepriced_restore_contract import inventory
    assert manifest['original_sources']==inventory()['sources']
    assert len(manifest['original_sources'])==115 and len(manifest['sql_paths'])==24
    assert manifest['producer_sources']=={'core/schema/prepriced_restore_contract.py':hashlib.sha256((BASE.parent/'prepriced_restore_contract.py').read_bytes()).hexdigest()}
    assert len(list(path for path in bundle.rglob('*') if path.is_file()))==116
    tree=ast.parse((bundle/'core/schema/bootstrap.py').read_text())
    assert any(isinstance(n,ast.FunctionDef) and n.name=='_bootstrap_database_contract' for n in tree.body)
    assert b'027_power_target_operations' in (bundle/'core/schema/migrator.py').read_bytes()
    assert b'028_' not in (bundle/'core/schema/migrator.py').read_bytes()
    assert len(p._PROFILE['ledger'])==52


@pytest.mark.parametrize('change',['missing','new','changed','file_symlink','directory_symlink','manifest_changed'])
def test_package_tamper_refuses_before_import(tmp_path,change):
    target=tmp_path/'package';shutil.copytree(BASE,target)
    chosen=target/'bundle/core/schema/migrator.py'
    if change=='missing':chosen.unlink()
    elif change=='new':(target/'bundle/core/evil.py').write_text('raise Exception()')
    elif change=='changed':chosen.write_bytes(chosen.read_bytes()+b'\n# drift\n')
    elif change=='file_symlink':chosen.unlink();chosen.symlink_to(BASE/'bundle/core/schema/migrator.py')
    elif change=='directory_symlink':shutil.rmtree(target/'bundle/core/migrations');(target/'bundle/core/migrations').symlink_to(BASE/'bundle/core/migrations',target_is_directory=True)
    else:(target/'manifest.json').write_text('{}')
    with pytest.raises(integrity.Preserved027Error):integrity.check_package(target)


def isolated(code,*,cwd=None,env=None):
    return subprocess.run([sys.executable,'-I','-c',code],cwd=cwd,env=env,capture_output=True,text=True,timeout=10)


def runner_import_code(tail):
    return "import importlib.util,sys;sys.dont_write_bytecode=True;s=importlib.util.spec_from_file_location('owned_runner',"+repr(str(BASE/'runner.py'))+");r=importlib.util.module_from_spec(s);s.loader.exec_module(r);"+tail


def test_real_isolated_import_ignores_live_checkout_pythonpath_and_cwd_shadow(tmp_path):
    (tmp_path/'core').mkdir();(tmp_path/'core/__init__.py').write_text("raise RuntimeError('live shadow imported')")
    result=isolated(runner_import_code("b,m=r.prepare_sources();import core.base;assert core.base.__file__==str(b/'core/base.py');print('closed-origin')"),cwd=tmp_path,env={**os.environ,'PYTHONPATH':str(tmp_path),'PYTHONHOME':''})
    assert result.returncode==0,result.stderr
    assert result.stdout=='closed-origin\n'


def test_missing_dependency_cannot_use_live_checkout_fallback():
    result=isolated(runner_import_code("r.prepare_sources();import core.dependencies"),env={**os.environ,'PYTHONPATH':str(BASE.parents[2])})
    assert result.returncode!=0 and 'dependency is not admitted' in result.stderr


def test_existing_live_module_loaded_before_setup_refuses():
    result=isolated(runner_import_code("import types;sys.modules['core']=types.ModuleType('core');r.prepare_sources()"))
    assert result.returncode!=0 and 'imported early' in result.stderr


def test_importpath_replacement_after_setup_refuses(tmp_path):
    (tmp_path/'core').mkdir();(tmp_path/'core/__init__.py').write_text('')
    result=isolated(runner_import_code("r.prepare_sources();sys.path.insert(0,"+repr(str(tmp_path))+");import core"))
    assert result.returncode!=0 and 'origin differs' in result.stderr


def test_unisolated_runner_refuses_before_backend_import():
    import importlib.util
    s=importlib.util.spec_from_file_location('test_unisolated',BASE/'runner.py');r=importlib.util.module_from_spec(s);s.loader.exec_module(r)
    prior=list(sys.path)
    try:
        with pytest.raises(r.Preserved027Error,match='isolated'):r.prepare_sources()
    finally:
        sys.path[:]=[x for x in prior if x!=str(BASE)]
        sys.modules.pop('integrity',None)


def test_unknown_action_control_refuses_without_connecting(tmp_path):
    control=tmp_path/'control';control.write_text(json.dumps({'action':'executeSQL'}));control.chmod(0o600)
    result=subprocess.run([sys.executable,'-I',str(BASE/'runner.py'),str(control)],capture_output=True,text=True,timeout=10)
    assert result.returncode==1 and result.stdout=='' and result.stderr=='preserved027-refused:Preserved027Error\n'


@pytest.mark.parametrize('change',['mode','symlink','duplicate','oversize','expired','bool_oid','extra','foreign_name'])
def test_private_control_closed_shape_and_identity(tmp_path,change):
    control=tmp_path/'control';data={'action':'build027','database':'odin_validate_'+'a'*20,'oid':1,'owner':2,'url':'postgresql+psycopg://postgres@pg/odin_validate_'+'a'*20,'password_file':'/not/a/credential','deadline':monotonic()+10}
    if change=='expired':data['deadline']=monotonic()-1
    if change=='bool_oid':data['oid']=True
    if change=='extra':data['sql']='SELECT 1'
    if change=='foreign_name':data['database']='postgres'
    raw=json.dumps(data)
    if change=='duplicate':raw='{"action":"build027",'+raw[1:]
    if change=='oversize':raw=' '*4097
    control.write_text(raw);control.chmod(0o644 if change=='mode' else 0o600)
    if change=='symlink':target=tmp_path/'actual';control.rename(target);control.symlink_to(target)
    result=isolated(runner_import_code("r.read_control("+repr(str(control))+")"))
    assert result.returncode!=0


def test_arbitrary_executor_or_forged_stage_refuses():
    with pytest.raises(p.Preserved027Error):p._mint_owned_stage(object(),'odin_validate_'+'a'*20,deadline=monotonic()+10)
    with pytest.raises(p.Preserved027Error):p._verify({},deadline=monotonic()+10)
    forged=p._Owned027Stage(None,'odin_validate_'+'a'*20,1,2,object(),{'state':'new'})
    with pytest.raises(p.Preserved027Error):p._verify(forged,deadline=monotonic()+10)


def test_frozen_parent_lexical_comparison_preserves_quoted_literals():
    from core.schema.preserved027.catalogue import sql_statement_sequence
    assert sql_statement_sequence("BEGIN  RETURN 'same'; END;")==sql_statement_sequence("BEGIN RETURN 'same'; END;")
    assert sql_statement_sequence("BEGIN RETURN 'same'; END;")!=sql_statement_sequence("BEGIN RETURN 'other'; END;")
    with pytest.raises(p.Preserved027Error):sql_statement_sequence("BEGIN RETURN 'broken;")


def test_drift_after_package_check_refuses_before_source_execution(tmp_path):
    copy=tmp_path/'copy';shutil.copytree(BASE,copy)
    code=runner_import_code("b,m=r.prepare_sources("+repr(str(copy))+");target=b/'core/base.py';target.write_bytes(target.read_bytes()+b'\\n# after-check drift\\n');import core.base")
    result=isolated(code)
    assert result.returncode!=0 and 'import source integrity differs' in result.stderr

@pytest.mark.parametrize('mode',['success','stderr','nonzero','wrong_ack','oversized','timeout'])
def test_real_owned_child_supervision_closed_ack_output_deadline_and_reap(tmp_path,monkeypatch,mode):
    actual_popen=subprocess.Popen
    scripts={'success':"print('preserved027-validated',flush=True)",
             'stderr':"import sys;print('preserved027-validated');print('uncertain',file=sys.stderr)",
             'nonzero':"raise SystemExit(2)",
             'wrong_ack':"print('not-an-ack')",
             # Keep this child live so the test proves supervisor termination rather than
             # racing an already-exited process group on Darwin.
             'oversized':"import time;print('x'*20000,flush=True);time.sleep(60)",
             'timeout':"import time;time.sleep(5)"}
    child=tmp_path/'owned_child.py';child.write_text(scripts[mode])
    process=[]
    def launch(argv,**kw):
        assert argv[:2]==[sys.executable,'-I'] and argv[2]==str(BASE/'runner.py')
        assert kw['start_new_session'] is True and 'PYTHONPATH' not in kw['env']
        owned=actual_popen([sys.executable,'-I',str(child)],**kw);process.append(owned);return owned
    monkeypatch.setattr(p.subprocess,'Popen',launch)
    control=tmp_path/'control';control.write_text('{}')
    if mode=='success':p._bounded_child(control,monotonic()+5)
    else:
        expected='child output exceeded' if mode=='oversized' else None
        with pytest.raises(p.Preserved027Error,match=expected):p._bounded_child(control,monotonic()+(.15 if mode=='timeout' else 5))
    assert process[0].returncode is not None
    if mode=='oversized':assert process[0].returncode == -signal.SIGKILL
    assert process[0].stdout.closed and process[0].stderr.closed


def test_parent_does_not_accept_child_ack_without_fresh_catalogue(tmp_path,monkeypatch):
    from sqlalchemy import create_engine
    engine=create_engine('sqlite://')
    stage=p._Owned027Stage(engine,'odin_validate_'+'a'*20,1,2,p._TOKEN,{'state':'new'})
    p._STAGES.add(stage)
    password=tmp_path/'password';password.write_text('owned-test-only');password.chmod(0o600)
    observations=[]
    original_verify=p._verify
    monkeypatch.setattr(p,'_verify',lambda *a,**k:observations.append('parent-identity'))
    monkeypatch.setattr(p,'_bounded_child',lambda *a,**k:observations.append('child-ack'))
    # Actual fresh parent Connection attempts required PostgreSQL settings. It
    # fails on this explicitly synthetic SQLite executor; stdout cannot admit it.
    with pytest.raises(Exception):p._run_stage(stage,'build027',password_file=password,deadline=monotonic()+5)
    assert observations==['parent-identity','child-ack','parent-identity']
    assert stage.progress['state']=='uncertain'
    with pytest.raises(p.Preserved027Error):
        # Restore real capability verification: an uncertain stage never reuses.
        original_verify(stage,deadline=monotonic()+5)
    engine.dispose()
