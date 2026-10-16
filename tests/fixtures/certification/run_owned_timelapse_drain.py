"""Real internal drain process-death/restart; pytest-owned temp DB/media only."""
import json,os,sys,tempfile
from pathlib import Path

try:
    database=Path(sys.argv[1]).resolve();root=Path(sys.argv[2]).resolve()
    assert database.name=='review.db' and root.name=='owned-media'
    assert database.parent==root.parent and database.is_relative_to(Path(os.environ.get('ODIN_OWNED_TEST_ROOT') or tempfile.gettempdir()).resolve())
    assert database.is_file() and root.is_dir()
    tid=int(sys.argv[3]);target=sys.argv[4]
    assert tid>0 and target in ('file','directory','sync','final_ack','none','receipt')
    os.environ['DATABASE_URL']='sqlite:///'+str(database);os.environ.pop('API_KEY',None)
    from sqlalchemy import create_engine,text
    from sqlalchemy.orm import Session
    from core.schema.bootstrap import import_all_models
    import_all_models()
    from modules.archives import timelapse_drain_transactions as transactions
    from modules.archives.timelapse_drain import drain_retirement,retirement_receipt
    engine=create_engine('sqlite:///'+str(database),hide_parameters=True)
    principal={'id':3,'role':'admin','group_id':1,'_auth_kind':'session_jwt'}
    actual_unlink=os.unlink;actual_rmdir=os.rmdir;actual_sync=os.fsync
    def unlink(*args,**kwargs):
        actual_unlink(*args,**kwargs)
        if target=='file':os._exit(74)
    def rmdir(*args,**kwargs):
        actual_rmdir(*args,**kwargs)
        if target=='directory':os._exit(74)
    def sync(fd):
        actual_sync(fd)
        if target=='sync':os._exit(75)
    class FinalAcknowledgmentLost(Session):
        def commit(self):
            complete=self.execute(text("SELECT COUNT(*) FROM timelapse_retirements WHERE state='complete'")).scalar_one()>0
            super().commit()
            if complete:os._exit(76)
    os.unlink=unlink;os.rmdir=rmdir;os.fsync=sync
    if target=='final_ack':transactions.Session=FinalAcknowledgmentLost
    try:
        receipt=retirement_receipt(engine,sys.argv[5],principal) if target=='receipt' else drain_retirement(engine,root,tid,principal)
    finally:engine.dispose()
    print(json.dumps({'status':receipt.state,'operation_id':receipt.operation_id}))
except Exception as error:
    print(json.dumps({'status':'FAIL','error_type':type(error).__name__}))
    raise SystemExit(2)
