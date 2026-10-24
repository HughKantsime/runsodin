"""Fixed isolated029 archive stage. Only a private control file selects inputs.

Runs the preserved restore sources (never the live checkout) to validate one
released037 PostgreSQL archive and import its data into a fresh stage
database the parent created and owns. The parent then upgrades that stage with
live sources. Archive SQL is never executed; only allowlisted data is imported.
"""
import hashlib
import importlib.abc
import importlib.machinery
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import uuid
from time import monotonic

# -I removes cwd/PYTHONPATH/user-site; explicitly admitted source is checked first.
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parent))
from integrity import check_package,Preserved037Error

MAX_SECONDS=3600
_NAME=re.compile('odin_validate_[0-9a-f]{20}')


class _PinnedLoader(importlib.machinery.SourceFileLoader):
    def __init__(self,name,path,digest):
        super().__init__(name,str(path));self.expected=digest
    def get_code(self,fullname):
        path=Path(self.path)
        metadata=path.lstat()
        if not stat.S_ISREG(metadata.st_mode):raise Preserved037Error('Preserved037 import source is unsafe')
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        try:
            before=os.fstat(fd)
            if (before.st_dev,before.st_ino)!=(metadata.st_dev,metadata.st_ino) or before.st_size>4194304:raise Preserved037Error('Preserved037 import source identity differs')
            blocks=[];count=0
            while True:
                block=os.read(fd,65536)
                if not block:break
                count+=len(block)
                if count>4194304:raise Preserved037Error('Preserved037 import source exceeds bound')
                blocks.append(block)
            raw=b''.join(blocks);after=os.fstat(fd)
            if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns) or hashlib.sha256(raw).hexdigest()!=self.expected:
                raise Preserved037Error('Preserved037 import source integrity differs')
        finally:os.close(fd)
        return compile(raw,self.path,'exec',dont_inherit=True)


class _ClosedSources(importlib.abc.MetaPathFinder):
    def __init__(self,bundle,manifest):
        self.bundle=bundle
        self.digests=dict(manifest['sources']);self.paths=set(self.digests)
    def find_spec(self,fullname,path=None,target=None):
        if fullname.split('.')[0] not in ('core','modules'):return None
        relative=fullname.replace('.','/')
        selected=relative+'/__init__.py' if relative+'/__init__.py' in self.paths else relative+'.py'
        if selected not in self.paths:raise Preserved037Error('Preserved037 dependency is not admitted: '+fullname)
        spec=importlib.machinery.PathFinder.find_spec(fullname,path)
        if spec is None or spec.origin is None or Path(spec.origin)!=self.bundle/selected:
            raise Preserved037Error('Preserved037 import origin differs')
        spec.loader=_PinnedLoader(fullname,self.bundle/selected,self.digests[selected])
        return spec


def prepare_sources(base=None):
    bundle,manifest=check_package(base)
    if not sys.flags.isolated:raise Preserved037Error('Preserved037 isolated interpreter required')
    for name in tuple(sys.modules):
        if name.split('.')[0] in ('core','modules'):raise Preserved037Error('Preserved037 backend was imported early')
    sys.path.insert(0,str(bundle))
    sys.meta_path.insert(0,_ClosedSources(bundle,manifest))
    return bundle,manifest


def _pairs(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise Preserved037Error('Preserved037 duplicate control key')
        result[key]=value
    return result


_REQUIRED={'action','archive','archive_sha256','target_url','password_file','maintenance_url','maintenance_password_file','directory','journal','database','oid','owner','stage_url','deadline'}


def _private_file(path,label):
    facts=Path(path).lstat()
    if not stat.S_ISREG(facts.st_mode) or facts.st_uid!=os.getuid() or stat.S_IMODE(facts.st_mode)&0o077 or facts.st_nlink!=1:
        raise Preserved037Error('Preserved037 '+label+' is unsafe')


def read_control(path):
    path=Path(path)
    metadata=path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid!=os.getuid() or stat.S_IMODE(metadata.st_mode)!=0o600 or metadata.st_nlink!=1 or metadata.st_size>8192:
        raise Preserved037Error('Preserved037 control is unsafe')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        current=os.fstat(fd)
        if (current.st_dev,current.st_ino,current.st_size)!=(metadata.st_dev,metadata.st_ino,metadata.st_size):raise Preserved037Error('Preserved037 control identity differs')
        raw=os.read(fd,8193)
        if len(raw)!=metadata.st_size:raise Preserved037Error('Preserved037 control size differs')
    finally:os.close(fd)
    value=json.loads(raw,object_pairs_hook=_pairs)
    if type(value) is not dict or set(value)!=_REQUIRED or value['action']!='restore037' or type(value['database']) is not str or not _NAME.fullmatch(value['database']):
        raise Preserved037Error('Preserved037 control shape differs')
    if any(type(value[k]) is not int or type(value[k]) is bool or value[k]<=0 for k in ('oid','owner')) or type(value['deadline']) not in (int,float) or not math.isfinite(value['deadline']) or not 0<value['deadline']-monotonic()<=MAX_SECONDS:
        raise Preserved037Error('Preserved037 control bounds differ')
    if any(type(value[k]) is not str or not value[k] for k in _REQUIRED-{'oid','owner','deadline','action','database'}):
        raise Preserved037Error('Preserved037 connection control differs')
    if not re.fullmatch('[0-9a-f]{64}',value['archive_sha256']):raise Preserved037Error('Preserved037 archive digest differs')
    return value


# libpq parameters that carry a credential or load one from a file (service= reads
# pg_service.conf, which can hold a password; sslkey/sslcert load client keys), plus the
# ODIN-managed application_name.
CREDENTIAL_QUERY_KEYS=frozenset({'password','sslpassword','passfile','service','sslkey','sslcert','application_name'})


def admit_url(url,label):
    """Non-credential parameters (sslmode, connect_timeout, ...) pass, as on the current path."""
    if url.password is not None or CREDENTIAL_QUERY_KEYS & {str(key).lower() for key in url.query}:
        raise Preserved037Error('Preserved037 credential-bearing URL refused: '+label)
    return url


_CREATE=re.compile(r'\s*CREATE DATABASE "?(odin_validate_[0-9a-f]{20})"?\s*')


def _private_directory(path,label):
    facts=Path(path).lstat()
    if not stat.S_ISDIR(facts.st_mode) or facts.st_uid!=os.getuid() or stat.S_IMODE(facts.st_mode)&0o077:
        raise Preserved037Error('Preserved037 '+label+' is unsafe')


def journal_created_databases(journal):
    """Journal every validation database the preserved sources create (exec_driver_sql):
    ``<name>.intent`` before CREATE DATABASE runs, renamed to ``<name>`` once it succeeded,
    so a killed child's parent can drop exactly what this child created."""
    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    def sync(folder):
        descriptor=os.open(folder,os.O_RDONLY)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)
    def before(conn,cursor,statement,parameters,context,executemany):
        match=_CREATE.fullmatch(statement)
        if match:
            descriptor=os.open(Path(journal)/(match.group(1)+'.intent'),os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
            try:os.fsync(descriptor)
            finally:os.close(descriptor)
            sync(journal)
    def after(conn,cursor,statement,parameters,context,executemany):
        match=_CREATE.fullmatch(statement)
        if match:
            os.replace(Path(journal)/(match.group(1)+'.intent'),Path(journal)/match.group(1));sync(journal)
    event.listen(Engine,'before_cursor_execute',before)
    event.listen(Engine,'after_cursor_execute',after)


def _sha256(path):
    digest=hashlib.sha256()
    with open(path,'rb') as file:
        for block in iter(lambda:file.read(1048576),b''):digest.update(block)
    return digest.hexdigest()


def main():
    if len(sys.argv)!=2:raise Preserved037Error('Preserved037 control argument required')
    # Check exact package BEFORE configuration/imports or any DB access.
    bundle,manifest=prepare_sources()
    control=read_control(sys.argv[1])
    for label in ('password_file','maintenance_password_file'):_private_file(control[label],label)
    for label in ('journal','directory'):_private_directory(control[label],label)
    archive=Path(control['archive'])
    if not stat.S_ISREG(archive.lstat().st_mode) or _sha256(archive)!=control['archive_sha256']:raise Preserved037Error('Preserved037 archive identity differs')
    from sqlalchemy.engine import make_url
    for key in ('target_url','maintenance_url','stage_url'):admit_url(make_url(control[key]),key)
    stage=make_url(control['stage_url'])
    if stage.get_backend_name()!='postgresql' or stage.database!=control['database']:
        raise Preserved037Error('Preserved037 stage URL differs')
    with tempfile.TemporaryDirectory(prefix='preserved037-child-') as folder:
        # Settings read .env from cwd: use a private empty cwd and closed environment.
        os.chdir(folder)
        path=os.environ.get('PATH','/usr/local/bin:/usr/bin:/bin')
        os.environ.clear()
        os.environ.update(DATABASE_URL=control['stage_url'],DATABASE_PASSWORD_FILE=control['maintenance_password_file'],ODIN_DATA_DIR=folder,
                          ODIN_DB_ROLE='restore',JWT_SECRET_KEY=uuid.uuid4().hex+uuid.uuid4().hex,PATH=path,LANG='C.UTF-8',PYTHONWARNINGS='ignore')
        import warnings;warnings.simplefilter('ignore')
        from sqlalchemy import text,inspect
        from core.database_config import create_database_engine
        from core.schema import bootstrap_database,validate_schema
        from modules.system import postgres_backup_service as b
        from modules.system.postgres_canonical_executables import validate_executables
        directory=Path(control['directory'])
        journal_created_databases(control['journal'])
        identity=(control['database'],control['oid'],control['owner'])
        engine=create_database_engine(control['stage_url'],role='restore',password_file=control['maintenance_password_file'])
        def verify(pristine=False):
            if monotonic()>=control['deadline']:raise Preserved037Error('Preserved037 deadline exhausted')
            with engine.connect() as c:
                if tuple(c.execute(text('SELECT current_database(),oid,datdba FROM pg_database WHERE datname=current_database()')).one())!=identity:
                    raise Preserved037Error('Preserved037 stage identity differs')
                if pristine and inspect(c).get_table_names(schema='public'):raise Preserved037Error('Preserved037 stage is not fresh')
        try:
            verify(pristine=True)
            args=dict(password_file=control['password_file'],maintenance_password_file=control['maintenance_password_file'])
            def load(candidate):
                # The preserved sources' own full current-format validation, then data
                # import into the fresh owned stage exactly as their apply path does.
                b._validate_current_postgres_restore(candidate,control['target_url'],control['maintenance_url'],base_dir=directory.parent,**args)
                verify(pristine=True)
                bootstrap_database(engine)
                b._restore_postgres_archive(candidate,control['stage_url'],control['maintenance_password_file'],directory)
            # Parent routes only exact released037 ledgers here. The frozen
            # current-format validator checks archive shape/checksums itself;
            # this child never invokes older historical routing or sources.
            load(archive);era=37
            verify()
            with engine.connect() as c:
                validate_schema(c)
                validate_executables(c)
                if set(inspect(c).get_table_names(schema='public'))!=set(b.canonical_tables()):raise Preserved037Error('Preserved037 stage tables differ')
                ids={row[0] for row in c.execute(text('SELECT migration_id FROM odin_schema_migrations'))}
                if ids!=set(manifest['ledger_ids']):raise Preserved037Error('Preserved037 stage ledger differs')
            if _sha256(archive)!=control['archive_sha256']:raise Preserved037Error('Preserved037 archive changed')
            check_package()
        finally:
            engine.dispose()
    print('preserved037-validated:%d'%era,flush=True)


# Exit codes (sysexits) carry the failure category to the parent; the text is only detail.
EXIT_ARCHIVE=65      # EX_DATAERR: the archive's content was judged and refused
EXIT_RETRY=75        # EX_TEMPFAIL: a server/client failure a later attempt can clear
EXIT_ENVIRONMENT=78  # EX_CONFIG: credentials, clients or the package are not usable
EXIT_INTERNAL=70     # EX_SOFTWARE: anything else


def exit_code_for(error):
    """Classify by exception type and cause, never by message text."""
    import subprocess  # nosec B404
    name=type(error).__name__
    cause=error.__cause__
    if name in ('BackupValidationError','MigrationError') and isinstance(cause,subprocess.TimeoutExpired):return EXIT_RETRY
    if name in ('BackupValidationError','MigrationError') and isinstance(cause,subprocess.CalledProcessError):
        command=[str(item) for item in (cause.cmd if isinstance(cause.cmd,(list,tuple)) else [cause.cmd])]
        return EXIT_RETRY if any(item=='--dbname' or item.startswith('--dbname=') for item in command) else EXIT_ARCHIVE
    if name in ('BackupValidationError','MigrationError') and isinstance(cause,OSError):return EXIT_ENVIRONMENT
    if name in ('BackupValidationError','MigrationError'):return EXIT_ARCHIVE
    if isinstance(error,Preserved037Error):
        return EXIT_ARCHIVE if str(error).startswith(('Preserved037 stage tables differ','Preserved037 stage ledger differs')) else EXIT_ENVIRONMENT
    if name in ('OperationalError','InterfaceError','DisconnectionError') or isinstance(error,(ConnectionError,TimeoutError)):return EXIT_RETRY
    return EXIT_INTERNAL


if __name__=='__main__':
    try:main()
    except BaseException as error:
        # Typed, source-authored refusal text only; never driver messages/SQL/params.
        name=type(error).__name__
        detail=''
        if name in ('BackupValidationError','MigrationError','Preserved037Error'):
            detail=':'+re.sub(r'[^ -~]','?',str(error))[:300]
        print('preserved037-refused:'+name+detail,file=sys.stderr,flush=True)
        raise SystemExit(exit_code_for(error)) from None
