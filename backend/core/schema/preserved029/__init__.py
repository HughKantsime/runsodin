"""Owned isolated029 archive-stage bridge. Not an ordinary bootstrap/restore entry point.

The parent creates a fresh ``odin_validate_*`` database, the isolated child (runner.py,
preserved sources only) validates the archive and imports its data there, and the parent
re-checks the stage identity before live sources upgrade it.
"""
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
from sqlalchemy import inspect,text
from sqlalchemy.engine import Engine
from .integrity import check_package,Preserved029Error

_BASE=Path(__file__).resolve().parent
MAX_SECONDS=3600
_OUTPUT_LIMIT=8192
_ACK=re.compile(rb'preserved029-validated:(25|26|27)\n')
_REFUSAL=re.compile(r'preserved029-refused:([A-Za-z0-9_]+)(?::(.*))?\n?')


class Preserved029Refused(Preserved029Error):
    """Child refusal; ``kind`` and ``detail`` are source-authored, never driver text."""
    def __init__(self,kind,detail):
        super().__init__(detail or 'Preserved029 child refused: '+kind)
        self.kind=kind;self.detail=detail


def ledger_ids():
    """Exact 027-era ledger the preserved sources produce (literal, integrity-checked)."""
    return frozenset(check_package()[1]['ledger_ids'])


def _identity(c):
    return tuple(c.execute(text('SELECT current_database(),oid,datdba FROM pg_database WHERE datname=current_database()')).one())


def _deadline(deadline):
    if type(deadline) not in (int,float) or not math.isfinite(deadline) or not 0<deadline-monotonic()<=MAX_SECONDS:
        raise Preserved029Error('Preserved029 deadline bounds differ')


def _bounded_child(control_path,deadline):
    _deadline(deadline)
    env={'PATH':os.environ.get('PATH','/usr/local/bin:/usr/bin:/bin'),'LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'}
    process=subprocess.Popen([sys.executable,'-I',str(_BASE/'runner.py'),str(control_path)],cwd=control_path.parent,env=env,
                             stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
    streams=selectors.DefaultSelector();payload={};total=0;failure=None;reaped=False
    try:
        for pipe in (process.stdout,process.stderr):
            streams.register(pipe,selectors.EVENT_READ);payload[pipe]=bytearray()
        while streams.get_map():
            remaining=deadline-monotonic()
            if remaining<=0:raise Preserved029Error('Preserved029 child deadline exhausted')
            for key,_ in streams.select(min(remaining,.2)):
                block=os.read(key.fileobj.fileno(),4096)
                if not block:streams.unregister(key.fileobj);continue
                total+=len(block)
                if total>_OUTPUT_LIMIT:raise Preserved029Error('Preserved029 child output exceeded')
                payload[key.fileobj].extend(block)
        remaining=deadline-monotonic()
        if remaining<=0:raise Preserved029Error('Preserved029 child deadline exhausted')
        process.wait(timeout=remaining);reaped=True
        out=bytes(payload[process.stdout]);err=bytes(payload[process.stderr])
        match=_ACK.fullmatch(out)
        if process.returncode==0 and match and not err:return int(match.group(1))
        refusal=_REFUSAL.fullmatch(err.decode('ascii','replace'))
        if process.returncode!=0 and not out and refusal:raise Preserved029Refused(refusal.group(1),refusal.group(2))
        raise Preserved029Error('Preserved029 child did not validate')
    except BaseException as error:
        failure=error
        raise
    finally:
        streams.close()
        if not reaped:
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            try:process.wait(timeout=5);reaped=True
            except BaseException:
                if failure is not None:failure.add_note('Owned preserved029 child reap unconfirmed')
                else:raise Preserved029Error('Owned preserved029 child reap unconfirmed') from None
        for pipe in (process.stdout,process.stderr):
            try:pipe.close()
            except BaseException:
                if failure is not None:failure.add_note('Owned preserved029 pipe close unconfirmed')
                else:raise Preserved029Error('Owned preserved029 pipe close unconfirmed') from None


def _private_file(path,label):
    facts=Path(path).lstat()
    if not stat.S_ISREG(facts.st_mode) or facts.st_uid!=os.getuid() or stat.S_IMODE(facts.st_mode)&0o077 or facts.st_nlink!=1:
        raise Preserved029Error('Preserved029 '+label+' is unsafe')


def restore_into_stage(engine,name,*,archive,archive_sha256,target_url,password_file,maintenance_url,maintenance_password_file,directory,deadline):
    """Fixed action on an exact fresh owned DB. Returns the archive's schema era (25/26/27).

    The caller owns and discards the stage on any failure.
    """
    check_package()
    _deadline(deadline)
    if not isinstance(engine,Engine) or engine.dialect.name!='postgresql' or type(name) is not str or not re.fullmatch('odin_validate_[0-9a-f]{20}',name) or engine.url.database!=name:
        raise Preserved029Error('Preserved029 owned stage required')
    for path,label in ((password_file,'credential'),(maintenance_password_file,'maintenance credential')):_private_file(path,label)
    if Path(password_file).resolve()==Path(maintenance_password_file).resolve():raise Preserved029Error('Preserved029 credentials must be distinct')
    with engine.connect() as c:
        identity=_identity(c)
        if identity[0]!=name:raise Preserved029Error('Preserved029 stage name differs')
        if inspect(c).get_table_names(schema='public'):raise Preserved029Error('Preserved029 stage is not fresh')
    url=engine.url.set(password=None)
    if url.query:raise Preserved029Error('Preserved029 URL query refused')
    with tempfile.TemporaryDirectory(prefix='preserved029-parent-') as folder:
        control=Path(folder)/'control.json'
        data={'action':'restore029','archive':str(Path(archive).resolve()),'archive_sha256':archive_sha256,
              'target_url':target_url,'password_file':str(Path(password_file).resolve()),
              'maintenance_url':maintenance_url,'maintenance_password_file':str(Path(maintenance_password_file).resolve()),
              'directory':str(Path(directory).resolve()),'database':name,'oid':identity[1],'owner':identity[2],
              'stage_url':url.render_as_string(hide_password=True),'deadline':deadline}
        descriptor=os.open(control,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(descriptor,'wb') as file:
            file.write(json.dumps(data,separators=(',',':')).encode());file.flush();os.fsync(file.fileno())
        era=_bounded_child(control,deadline)
    # Child stdout is insufficient: re-check package and the same owned DB identity.
    check_package()
    with engine.connect() as c:
        if _identity(c)!=identity:raise Preserved029Error('Preserved029 parent identity differs')
        ids={row[0] for row in c.execute(text('SELECT migration_id FROM odin_schema_migrations'))}
        if ids!=ledger_ids():raise Preserved029Error('Preserved029 stage ledger differs')
    if monotonic()>=deadline:raise Preserved029Error('Preserved029 parent deadline exhausted')
    return era
