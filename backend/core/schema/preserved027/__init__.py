"""Owned isolated027 stage bridge. Not an ordinary bootstrap/restore entry point."""
import ast
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
from time import monotonic
import weakref
from sqlalchemy import inspect,text
from sqlalchemy.engine import Engine
from .integrity import check_package,Preserved027Error

_TOKEN=object()
_STAGES=weakref.WeakSet()
_BASE=Path(__file__).resolve().parent


def _literal_profile():
    bundle,_=check_package()
    tree=ast.parse((bundle/'core/schema/prepriced_restore_contract.py').read_text())
    assignments={n.targets[0].id:ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign) and len(n.targets)==1 and isinstance(n.targets[0],ast.Name) and n.targets[0].id in ('PROFILE_JSON','CATALOG_QUERIES')}
    return json.loads(assignments['PROFILE_JSON']),assignments['CATALOG_QUERIES']

_PROFILE,CATALOG_QUERIES=_literal_profile()

@dataclass(frozen=True,eq=False)
class _Owned027Stage:
    engine:Engine
    name:str
    oid:int
    owner:int
    token:object
    progress:dict


def _finite(c,deadline):
    if type(deadline) not in (int,float) or not math.isfinite(deadline) or not 0<deadline-monotonic()<=600:raise Preserved027Error('Preserved027 deadline bounds differ')
    remaining=max(1,int((deadline-monotonic())*1000))
    c.execute(text("SELECT set_config('statement_timeout',CAST(least(CASE WHEN setting::integer>0 THEN setting::integer ELSE :remaining END,:remaining) AS text),true) FROM pg_settings WHERE name='statement_timeout'"),{'remaining':remaining})
    c.execute(text("SELECT set_config('lock_timeout',CAST(least(CASE WHEN setting::integer>0 THEN setting::integer ELSE 2000 END,2000) AS text),true) FROM pg_settings WHERE name='lock_timeout'"))


def _identity(c):
    return tuple(c.execute(text('SELECT current_database(),oid,datdba FROM pg_database WHERE datname=current_database()')).one())


def _pristine(c):
    count=c.execute(text("SELECT (SELECT count(*) FROM pg_class r JOIN pg_namespace n ON n.oid=r.relnamespace WHERE n.nspname='public')+(SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public')+(SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace WHERE n.nspname='public')")).scalar_one()
    if count!=0:raise Preserved027Error('Preserved027 stage is not pristine')


def _mint_owned_stage(engine,name,*,deadline):
    """Only internally created disposable engines; no URL/archive capability."""
    check_package()
    if not isinstance(engine,Engine) or engine.dialect.name!='postgresql' or type(name) is not str or not re.fullmatch('odin_validate_[0-9a-f]{20}',name) or engine.url.database!=name:
        raise Preserved027Error('Preserved027 owned stage required')
    with engine.connect() as c:
        _finite(c,deadline)
        identity=_identity(c)
        if identity[0]!=name:raise Preserved027Error('Preserved027 stage name differs')
        _pristine(c)
    stage=_Owned027Stage(engine,name,identity[1],identity[2],_TOKEN,{'state':'new'})
    _STAGES.add(stage)
    return stage


def _verify(stage,*,deadline,pristine=False):
    if type(stage) is not _Owned027Stage or stage not in _STAGES or stage.token is not _TOKEN or stage.progress['state']=='uncertain' or stage.engine.url.database!=stage.name:
        raise Preserved027Error('Preserved027 stage capability expired')
    with stage.engine.connect() as c:
        _finite(c,deadline)
        if _identity(c)!=(stage.name,stage.oid,stage.owner):raise Preserved027Error('Preserved027 stage identity differs')
        if pristine:_pristine(c)
    return stage.engine


def _bounded_child(control_path,deadline):
    if type(deadline) not in (int,float) or not math.isfinite(deadline) or not 0<deadline-monotonic()<=600:raise Preserved027Error('Preserved027 deadline bounds differ')
    process=subprocess.Popen([sys.executable,'-I',str(_BASE/'runner.py'),str(control_path)],cwd=control_path.parent,env={'PATH':'/usr/local/bin:/usr/bin:/bin','LANG':'C.UTF-8'},stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
    streams=selectors.DefaultSelector();payload={};total=0;failure=None;reaped=False
    try:
        for pipe in (process.stdout,process.stderr):
            streams.register(pipe,selectors.EVENT_READ);payload[pipe]=bytearray()
        while streams.get_map():
            remaining=deadline-monotonic()
            if remaining<=0:raise Preserved027Error('Preserved027 child deadline exhausted')
            for key,_ in streams.select(min(remaining,.2)):
                block=os.read(key.fileobj.fileno(),4096)
                if not block:streams.unregister(key.fileobj);continue
                total+=len(block)
                if total>8192:raise Preserved027Error('Preserved027 child output exceeded')
                payload[key.fileobj].extend(block)
        remaining=deadline-monotonic()
        if remaining<=0:raise Preserved027Error('Preserved027 child deadline exhausted')
        process.wait(timeout=remaining);reaped=True
        if process.returncode!=0 or bytes(payload[process.stdout])!=b'preserved027-validated\n' or payload[process.stderr]:
            raise Preserved027Error('Preserved027 child did not validate')
    except BaseException as error:
        failure=error
        raise
    finally:
        streams.close()
        # Before wait/reap the original leader PID remains reserved, including
        # a zombie leader whose descendants still hold the captured pipes.
        if not reaped:
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            try:process.wait(timeout=5);reaped=True
            except BaseException:
                if failure is not None:failure.add_note('Owned preserved027 child reap unconfirmed')
                else:raise Preserved027Error('Owned preserved027 child reap unconfirmed') from None
        for pipe in (process.stdout,process.stderr):
            try:pipe.close()
            except BaseException:
                if failure is not None:failure.add_note('Owned preserved027 pipe close unconfirmed')
                else:raise Preserved027Error('Owned preserved027 pipe close unconfirmed') from None


def _run_stage(stage,action,*,password_file,deadline):
    """Fixed action on an exact minted owned DB; multiphase, caller discards on failure."""
    if action not in ('build027','validate027'):raise Preserved027Error('Preserved027 fixed action required')
    check_package()
    _verify(stage,deadline=deadline,pristine=action=='build027')
    if action=='build027' and stage.progress['state']!='new':raise Preserved027Error('Preserved027 build phase differs')
    password=Path(password_file)
    facts=password.lstat()
    if not stat.S_ISREG(facts.st_mode) or facts.st_uid!=os.getuid() or stat.S_IMODE(facts.st_mode)!=0o600 or facts.st_nlink!=1:
        raise Preserved027Error('Preserved027 credential is unsafe')
    # Credentials never appear in argv/control URL/child output; separate0600 file.
    url=stage.engine.url.set(password=None)
    if url.query:raise Preserved027Error('Preserved027 URL query refused')
    with tempfile.TemporaryDirectory(prefix='preserved027-parent-') as folder:
        control=Path(folder)/'control.json'
        data={'action':action,'database':stage.name,'oid':stage.oid,'owner':stage.owner,'url':url.render_as_string(hide_password=True),'password_file':str(password.resolve()),'deadline':deadline}
        descriptor=os.open(control,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(descriptor,'wb') as file:
            file.write(json.dumps(data,separators=(',',':')).encode());file.flush();os.fsync(file.fileno())
        try:
            _bounded_child(control,deadline)
            # Child stdout/COMMIT ACK is insufficient. Observe same owned DB with
            # a fresh parent checkout and independent literal catalogue oracle.
            check_package();_verify(stage,deadline=deadline)
            from .catalogue import validate_catalogue
            with stage.engine.begin() as c:
                remaining=deadline-monotonic()
                if remaining<=0:raise Preserved027Error('Preserved027 parent deadline exhausted')
                c.execute(text("SELECT set_config('statement_timeout',CAST(least(CASE WHEN setting::integer>0 THEN setting::integer ELSE :remaining END,:remaining) AS text),true) FROM pg_settings WHERE name='statement_timeout'"),{'remaining':max(1,int(remaining*1000))})
                c.execute(text("SELECT set_config('lock_timeout',CAST(least(CASE WHEN setting::integer>0 THEN setting::integer ELSE 2000 END,2000) AS text),true) FROM pg_settings WHERE name='lock_timeout'"))
                if _identity(c)!=(stage.name,stage.oid,stage.owner):raise Preserved027Error('Preserved027 parent identity differs')
                validate_catalogue(c,_PROFILE)
            if monotonic()>=deadline:raise Preserved027Error('Preserved027 parent deadline exhausted')
            stage.progress['state']='validated'
        except BaseException:
            stage.progress['state']='uncertain'
            raise
    return {'profile':'pre-priced-027','schema_version':27,'validated':True}
