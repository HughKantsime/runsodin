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
import shutil
import signal
import stat
import subprocess  # nosec B404
import sys
import tempfile
from time import monotonic
import uuid
from sqlalchemy import inspect,text
from sqlalchemy.engine import Engine,URL
from .integrity import check_package,Preserved037Error

_BASE=Path(__file__).resolve().parent
MAX_SECONDS=3600
_OUTPUT_LIMIT=8192
_ACK=re.compile(rb'preserved037-validated:(37)\n')
_REFUSAL=re.compile(r'preserved037-refused:([A-Za-z0-9_]+)(?::(.*))?\n?')


# The child's exit code (runner.exit_code_for) is the failure category.
CATEGORIES={65:'archive',75:'retry'}


class Preserved037Refused(Preserved037Error):
    """Child refusal. ``category`` comes from the exit code ('archive', 'retry' or
    'environment'); ``kind`` and ``detail`` are source-authored text, never driver text."""
    def __init__(self,kind,detail,category='environment'):
        super().__init__(detail or 'Preserved037 child refused: '+kind)
        self.kind=kind;self.detail=detail;self.category=category


def ledger_ids():
    """Exact released037 ledger the preserved sources produce (literal, integrity-checked)."""
    return frozenset(check_package()[1]['ledger_ids'])


def _identity(c):
    return tuple(c.execute(text('SELECT current_database(),oid,datdba FROM pg_database WHERE datname=current_database()')).one())


def _deadline(deadline):
    if type(deadline) not in (int,float) or not math.isfinite(deadline) or not 0<deadline-monotonic()<=MAX_SECONDS:
        raise Preserved037Error('Preserved037 deadline bounds differ')


def _bounded_child(control_path,deadline):
    _deadline(deadline)
    env={'PATH':os.environ.get('PATH','/usr/local/bin:/usr/bin:/bin'),'LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'}
    process=subprocess.Popen([sys.executable,'-I',str(_BASE/'runner.py'),str(control_path)],cwd=control_path.parent,env=env,
                             stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)  # nosec B603
    streams=selectors.DefaultSelector();payload={};total=0;failure=None;reaped=False
    try:
        for pipe in (process.stdout,process.stderr):
            streams.register(pipe,selectors.EVENT_READ);payload[pipe]=bytearray()
        while streams.get_map():
            remaining=deadline-monotonic()
            if remaining<=0:raise Preserved037Error('Preserved037 child deadline exhausted')
            for key,_ in streams.select(min(remaining,.2)):
                block=os.read(key.fileobj.fileno(),4096)
                if not block:streams.unregister(key.fileobj);continue
                total+=len(block)
                if total>_OUTPUT_LIMIT:raise Preserved037Error('Preserved037 child output exceeded')
                payload[key.fileobj].extend(block)
        remaining=deadline-monotonic()
        if remaining<=0:raise Preserved037Error('Preserved037 child deadline exhausted')
        process.wait(timeout=remaining);reaped=True
        out=bytes(payload[process.stdout]);err=bytes(payload[process.stderr])
        match=_ACK.fullmatch(out)
        if process.returncode==0 and match and not err:return int(match.group(1))
        refusal=_REFUSAL.fullmatch(err.decode('ascii','replace'))
        if process.returncode not in (0,None) and not out and refusal:
            raise Preserved037Refused(refusal.group(1),refusal.group(2),CATEGORIES.get(process.returncode,'environment'))
        raise Preserved037Error('Preserved037 child did not validate')
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
                if failure is not None:failure.add_note('Owned preserved037 child reap unconfirmed')
                else:raise Preserved037Error('Owned preserved037 child reap unconfirmed') from None
        for pipe in (process.stdout,process.stderr):
            try:pipe.close()
            except BaseException:
                if failure is not None:failure.add_note('Owned preserved037 pipe close unconfirmed')
                else:raise Preserved037Error('Owned preserved037 pipe close unconfirmed') from None


# libpq parameters that carry a credential or load one from a file (service= reads
# pg_service.conf, which can hold a password; sslkey/sslcert load client keys), plus the
# ODIN-managed application_name.
CREDENTIAL_QUERY_KEYS=frozenset({'password','sslpassword','passfile','service','sslkey','sslcert','application_name'})


def refuse_credential_query(url):
    """Same rule as the current restore path: non-credential parameters (sslmode, ...) pass."""
    if url.password is not None or CREDENTIAL_QUERY_KEYS & {str(key).lower() for key in url.query}:
        raise Preserved037Error('Preserved037 credential-bearing URL refused')
    return url


def credentialless(url):
    """The stage URL without any credential. URL.set(password=None) would keep it."""
    return refuse_credential_query(URL.create(url.drivername,username=url.username,host=url.host,port=url.port,database=url.database,query=url.query))


def _private_file(path,label):
    facts=Path(path).lstat()
    if not stat.S_ISREG(facts.st_mode) or facts.st_uid!=os.getuid() or stat.S_IMODE(facts.st_mode)&0o077 or facts.st_nlink!=1:
        raise Preserved037Error('Preserved037 '+label+' is unsafe')


_NAME=re.compile('odin_validate_[0-9a-f]{20}')


def _child_journal(journal,work):
    """The child's own journal directory: only names in it can be the child's databases."""
    folder=Path(journal)/work.name
    folder.mkdir(mode=0o700)
    return folder


def _journaled(folder):
    """(confirmed, intended): CREATE DATABASE succeeded, or was about to run."""
    confirmed=set();intended=set()
    for entry in Path(folder).iterdir():
        if _NAME.fullmatch(entry.name):confirmed.add(entry.name)
        elif entry.name.endswith('.intent') and _NAME.fullmatch(entry.name[:-7]):intended.add(entry.name[:-7])
    return confirmed,intended


def _supervised(data,deadline,*,journal,work,keep,drop,existing):
    """Run the child; whatever happens, drop the databases it created and remove its files.

    The child journals into its own directory (never another operation's names): an
    ``<name>.intent`` entry before each CREATE DATABASE and ``<name>`` once it succeeded.
    A confirmed name is the child's. An intended name is the child's only if no database
    of that name existed before the child started (``existing()``), so a database this
    code did not create is never dropped. ``drop`` returns the names it dropped.
    """
    folder=_child_journal(journal,work)
    before=set()
    data={**data,'journal':str(folder)}
    try:
        before=set(existing())
        with tempfile.TemporaryDirectory(prefix='preserved037-parent-') as control_folder:
            control=Path(control_folder)/'control.json'
            descriptor=os.open(control,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(descriptor,'wb') as file:
                file.write(json.dumps(data,separators=(',',':')).encode());file.flush();os.fsync(file.fileno())
            return _bounded_child(control,deadline)
    finally:
        try:
            confirmed,intended=_journaled(folder)
            targets=(confirmed|{name for name in intended if name not in before})-set(keep)
            dropped=set(drop(targets)) if targets else set()
            for name in dropped|{name for name in intended if name in before}:
                (folder/name).unlink(missing_ok=True);(folder/(name+'.intent')).unlink(missing_ok=True)
            if not any(folder.iterdir()):folder.rmdir()
        finally:
            shutil.rmtree(work,ignore_errors=True)


def restore_into_stage(engine,name,*,archive,archive_sha256,target_url,password_file,maintenance_url,maintenance_password_file,directory,journal,drop,existing,deadline):
    """Fixed action on an exact fresh owned DB. Returns the archive's schema era (37).

    The caller owns and discards the stage on any failure; the child's own validation
    databases and working files are removed here, including after a kill.
    """
    check_package()
    _deadline(deadline)
    if not isinstance(engine,Engine) or engine.dialect.name!='postgresql' or type(name) is not str or not re.fullmatch('odin_validate_[0-9a-f]{20}',name) or engine.url.database!=name:
        raise Preserved037Error('Preserved037 owned stage required')
    for path,label in ((password_file,'credential'),(maintenance_password_file,'maintenance credential')):_private_file(path,label)
    if Path(password_file).resolve()==Path(maintenance_password_file).resolve():raise Preserved037Error('Preserved037 credentials must be distinct')
    with engine.connect() as c:
        identity=_identity(c)
        if identity[0]!=name:raise Preserved037Error('Preserved037 stage name differs')
        if inspect(c).get_table_names(schema='public'):raise Preserved037Error('Preserved037 stage is not fresh')
    url=credentialless(engine.url)
    from sqlalchemy.engine import make_url
    for value in (target_url,maintenance_url):refuse_credential_query(make_url(value))
    work=Path(directory).resolve()/('.odin-preserved037-'+uuid.uuid4().hex[:20])
    (work/'backups').mkdir(mode=0o700,parents=True)
    data={'action':'restore037','archive':str(Path(archive).resolve()),'archive_sha256':archive_sha256,
          'target_url':target_url,'password_file':str(Path(password_file).resolve()),
          'maintenance_url':maintenance_url,'maintenance_password_file':str(Path(maintenance_password_file).resolve()),
          'directory':str(work/'backups'),'journal':str(Path(journal).resolve()),'database':name,'oid':identity[1],'owner':identity[2],
          'stage_url':url.render_as_string(hide_password=True),'deadline':deadline}
    era=_supervised(data,deadline,journal=journal,work=work,keep={name},drop=drop,existing=existing)
    # Child stdout is insufficient: re-check package and the same owned DB identity.
    check_package()
    with engine.connect() as c:
        if _identity(c)!=identity:raise Preserved037Error('Preserved037 parent identity differs')
        ids={row[0] for row in c.execute(text('SELECT migration_id FROM odin_schema_migrations'))}
        if ids!=ledger_ids():raise Preserved037Error('Preserved037 stage ledger differs')
    if monotonic()>=deadline:raise Preserved037Error('Preserved037 parent deadline exhausted')
    return era
