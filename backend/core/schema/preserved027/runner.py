"""Fixed isolated027 stage builder. Only a private control file selects action."""
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
from time import monotonic

# -I removes cwd/PYTHONPATH/user-site; explicitly admitted source is checked first.
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parent))
from integrity import check_package,Preserved027Error

class _PinnedLoader(importlib.machinery.SourceFileLoader):
    def __init__(self,name,path,digest):
        super().__init__(name,str(path));self.expected=digest
    def get_code(self,fullname):
        path=Path(self.path)
        metadata=path.lstat()
        if not stat.S_ISREG(metadata.st_mode):raise Preserved027Error('Preserved027 import source is unsafe')
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        try:
            before=os.fstat(fd)
            if (before.st_dev,before.st_ino)!=(metadata.st_dev,metadata.st_ino) or before.st_size>4194304:raise Preserved027Error('Preserved027 import source identity differs')
            blocks=[];count=0
            while True:
                block=os.read(fd,65536)
                if not block:break
                count+=len(block)
                if count>4194304:raise Preserved027Error('Preserved027 import source exceeds bound')
                blocks.append(block)
            raw=b''.join(blocks);after=os.fstat(fd)
            if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns) or hashlib.sha256(raw).hexdigest()!=self.expected:
                raise Preserved027Error('Preserved027 import source integrity differs')
        finally:os.close(fd)
        return compile(raw,self.path,'exec',dont_inherit=True)

class _ClosedSources(importlib.abc.MetaPathFinder):
    def __init__(self,bundle,manifest):
        self.bundle=bundle
        self.digests={**manifest['original_sources'],**manifest['producer_sources']};self.paths=set(self.digests)
    def find_spec(self,fullname,path=None,target=None):
        if fullname.split('.')[0] not in ('core','modules'):return None
        relative=fullname.replace('.','/')
        selected=relative+'/__init__.py' if relative+'/__init__.py' in self.paths else relative+'.py'
        if selected not in self.paths:raise Preserved027Error('Preserved027 dependency is not admitted')
        spec=importlib.machinery.PathFinder.find_spec(fullname,path)
        if spec is None or spec.origin is None or Path(spec.origin)!=self.bundle/selected:
            raise Preserved027Error('Preserved027 import origin differs')
        spec.loader=_PinnedLoader(fullname,self.bundle/selected,self.digests[selected])
        return spec

def prepare_sources(base=None):
    bundle,manifest=check_package(base)
    # No backend search path except this immutable bundle. Installed libraries
    # remain the interpreter's -I paths; protected namespaces cannot fall back.
    if not sys.flags.isolated:raise Preserved027Error('Preserved027 isolated interpreter required')
    for name in tuple(sys.modules):
        if name.split('.')[0] in ('core','modules'):raise Preserved027Error('Preserved027 backend was imported early')
    sys.path.insert(0,str(bundle))
    finder=_ClosedSources(bundle,manifest);sys.meta_path.insert(0,finder)
    return bundle,manifest

def _pairs(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise Preserved027Error('Preserved027 duplicate control key')
        result[key]=value
    return result

def read_control(path):
    path=Path(path)
    metadata=path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid!=os.getuid() or stat.S_IMODE(metadata.st_mode)!=0o600 or metadata.st_nlink!=1 or metadata.st_size>4096:
        raise Preserved027Error('Preserved027 control is unsafe')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        current=os.fstat(fd)
        if (current.st_dev,current.st_ino,current.st_size)!=(metadata.st_dev,metadata.st_ino,metadata.st_size):raise Preserved027Error('Preserved027 control identity differs')
        raw=os.read(fd,4097)
        if len(raw)!=metadata.st_size or len(raw)>4096:raise Preserved027Error('Preserved027 control size differs')
        after=os.fstat(fd)
        if (after.st_dev,after.st_ino,after.st_size,after.st_ctime_ns,after.st_mtime_ns)!=(current.st_dev,current.st_ino,current.st_size,current.st_ctime_ns,current.st_mtime_ns):raise Preserved027Error('Preserved027 control changed')
    finally:os.close(fd)
    value=json.loads(raw,object_pairs_hook=_pairs)
    required={'action','database','oid','owner','url','password_file','deadline'}
    if type(value) is not dict or set(value)!=required or value['action'] not in ('build027','validate027') or type(value['database']) is not str or not re.fullmatch('odin_validate_[0-9a-f]{20}',value['database']):
        raise Preserved027Error('Preserved027 control shape differs')
    if any(type(value[k]) is not int or value[k]<=0 for k in ('oid','owner')) or type(value['deadline']) not in (int,float) or not math.isfinite(value['deadline']) or not 0<value['deadline']-monotonic()<=600:
        raise Preserved027Error('Preserved027 control bounds differ')
    if any(type(value[k]) is not str or not value[k] for k in ('url','password_file')):raise Preserved027Error('Preserved027 connection control differs')
    return value

def main():
    if len(sys.argv)!=2:raise Preserved027Error('Preserved027 control argument required')
    # Check exact package BEFORE configuration/imports or any DB access.
    bundle,manifest=prepare_sources()
    control=read_control(sys.argv[1])
    from sqlalchemy import event,text,inspect
    from sqlalchemy.engine import make_url
    url=make_url(control['url'])
    if url.get_backend_name()!='postgresql' or url.database!=control['database'] or url.password is not None or url.query:
        raise Preserved027Error('Preserved027 target URL differs')
    password=Path(control['password_file']);facts=password.lstat()
    if not stat.S_ISREG(facts.st_mode) or facts.st_uid!=os.getuid() or stat.S_IMODE(facts.st_mode)!=0o600 or facts.st_nlink!=1:
        raise Preserved027Error('Preserved027 credential file is unsafe')
    with tempfile.TemporaryDirectory(prefix='preserved027-child-') as folder:
        # Original Settings reads .env from cwd. Use a private empty cwd and
        # closed environment, never the checkout/user's .env or PYTHONPATH.
        os.chdir(folder)
        os.environ.clear()
        os.environ.update(DATABASE_URL=control['url'],DATABASE_PASSWORD_FILE=str(password),ODIN_DATA_DIR=folder,ODIN_DB_ROLE='restore',JWT_SECRET_KEY='owned-preserved027-no-request-authority',PATH='/usr/local/bin:/usr/bin:/bin',LANG='C.UTF-8')
        from core.db import engine
        identity=(control['database'],control['oid'],control['owner'])
        def admit(c):
            remaining=control['deadline']-monotonic()
            if remaining<=0:raise Preserved027Error('Preserved027 deadline exhausted')
            # SET before any catalog statement. Preserve stricter positive bounds.
            c.execute(text("SELECT set_config('statement_timeout',CAST(least(CASE WHEN setting::integer>0 THEN setting::integer ELSE :remaining END,:remaining) AS text),true) FROM pg_settings WHERE name='statement_timeout'"),{'remaining':max(1,int(remaining*1000))})
            c.execute(text("SELECT set_config('lock_timeout',CAST(least(CASE WHEN setting::integer>0 THEN setting::integer ELSE 2000 END,2000) AS text),true) FROM pg_settings WHERE name='lock_timeout'"))
            if tuple(c.execute(text('SELECT current_database(),oid,datdba FROM pg_database WHERE datname=current_database()')).one())!=identity:
                raise Preserved027Error('Preserved027 stage identity differs')
        event.listen(engine,'begin',admit)
        try:
            with engine.connect() as c:
                if control['action']=='build027' and inspect(c).get_table_names(schema='public'):
                    raise Preserved027Error('Preserved027 build target is not fresh')
            from core.schema.bootstrap import bootstrap_database
            from core.schema.prepriced_restore_contract import validate_connection
            if control['action']=='build027':bootstrap_database(engine,backend_root=bundle)
            else:
                # Validate prior installation BEFORE recorded-migration repeat.
                with engine.connect() as c:validate_connection(c)
                bootstrap_database(engine,backend_root=bundle)
            with engine.connect() as c:validate_connection(c)
            if monotonic()>=control['deadline']:raise Preserved027Error('Preserved027 deadline exhausted')
            check_package()
        finally:
            event.remove(engine,'begin',admit);engine.dispose()
    print('preserved027-validated',flush=True)

if __name__=='__main__':
    try:main()
    except BaseException as error:
        # No raw driver messages/SQL/params/paths/config metadata.
        print('preserved027-refused:'+type(error).__name__,file=sys.stderr,flush=True)
        raise SystemExit(1) from None
