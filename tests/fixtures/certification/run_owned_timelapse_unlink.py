"""Owned process-death proof for FS primitive, not a drain transaction service."""
import json,os,sys,tempfile
from pathlib import Path

try:
    database=Path(sys.argv[1]).resolve();root=Path(sys.argv[2]).resolve()
    assert database.name=='review.db' and root.name=='owned-media'
    assert database.parent==root.parent and database.is_relative_to(Path(tempfile.gettempdir()).resolve())
    assert database.is_file() and root.is_dir()
    tid=int(sys.argv[3]);index=int(sys.argv[4]);crash=sys.argv[5]
    assert tid>0 and index>=0 and crash in ('unlink','sync','none')
    os.environ['DATABASE_URL']='sqlite:///'+str(database);os.environ.pop('API_KEY',None)
    from sqlalchemy import create_engine,text
    from sqlalchemy.orm import Session
    from core.schema.bootstrap import import_all_models
    import_all_models()
    from core.timelapse_manifest import _root
    from core.timelapse_media_lock import acquire_media_guard
    from core.timelapse_quarantine import quarantine_directory
    from core.timelapse_drain_fs import DrainFilesystem
    from modules.archives.timelapse_stage_transactions import load_stage_plan,directory_identity
    engine=create_engine('sqlite:///'+str(database),hide_parameters=True)
    try:
        with acquire_media_guard(root,tid) as guard:
            with _root(root) as (root_fd,identity):
                guard.assert_root_identity(identity)
                snapshot=load_stage_plan(engine,tid,{'id':3,'role':'admin','group_id':1,'_auth_kind':'session_jwt'},identity)
                with Session(engine) as db:
                    receipts={value['object_index']:dict(value) for value in db.execute(text('SELECT * FROM timelapse_retirement_unlinks WHERE operation_id=:operation'),{'operation':snapshot.operation}).mappings()}
                protected=directory_identity('.retirement',snapshot.quarantine_row)
                parents={path:directory_identity(path,row) for path,row in snapshot.parent_rows.items()}
                with quarantine_directory(root,identity,protected) as (fd,_):
                    fs=DrainFilesystem(root_fd,fd,identity,protected,snapshot.objects,parents,snapshot.move_rows,receipts,guard)
                    row=next(value for value in snapshot.objects if value['object_index']==index)
                    original_unlink=os.unlink;original_rmdir=os.rmdir;original_sync=os.fsync
                    def unlink(*args,**kwargs):
                        original_unlink(*args,**kwargs)
                        if crash=='unlink':os._exit(74)
                    def rmdir(*args,**kwargs):
                        original_rmdir(*args,**kwargs)
                        if crash=='unlink':os._exit(74)
                    def sync(fd):
                        original_sync(fd)
                        if crash=='sync':os._exit(75)
                    os.unlink=unlink;os.rmdir=rmdir;os.fsync=sync
                    fs.preflight();result=fs.unlink_and_sync(row)
    finally:engine.dispose()
    print(json.dumps({'status':result,'scope':'FS primitive; no progress/final database commits'}))
except Exception as error:
    print(json.dumps({'status':'FAIL','error_type':type(error).__name__}))
    raise SystemExit(2)
