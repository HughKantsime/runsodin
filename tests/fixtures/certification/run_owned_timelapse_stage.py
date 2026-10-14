"""Process-death/restart proof, restricted to the pytest-owned temp DB/media."""
import json,os,sys,tempfile
from pathlib import Path

try:
    database=Path(sys.argv[1]).resolve();root=Path(sys.argv[2]).resolve()
    trusted=Path(tempfile.gettempdir()).resolve()
    assert database.name=='review.db' and root.name=='owned-media'
    assert database.parent==root.parent and database.is_relative_to(trusted) and database.is_file() and root.is_dir()
    tid=int(sys.argv[3]);target=sys.argv[4]
    assert target in ('file','frames','root','none')
    os.environ['DATABASE_URL']='sqlite:///'+str(database);os.environ.pop('API_KEY',None)
    from sqlalchemy import create_engine
    from core.schema.bootstrap import import_all_models
    import_all_models()
    from core import timelapse_staging_fs as fs
    from modules.archives.timelapse_staging import stage_retirement
    diagnostic=os.environ.get('ODIN_OWNED_STAGE_DIAGNOSTIC')
    if diagnostic:
        import sqlite3
        with sqlite3.connect(database) as connection:
            journal=connection.execute('PRAGMA journal_mode').fetchone()[0]
        print(json.dumps({'event':'before_admission','core_db_loaded':'core.db' in sys.modules,'journal_mode':journal}),file=sys.stderr)
        if diagnostic=='preload':
            from core.dependencies import log_audit
            print(json.dumps({'event':'audit_preloaded','core_db_loaded':'core.db' in sys.modules}),file=sys.stderr)
        else:
            def trace(frame,event,arg):
                if event=='line' and frame.f_code.co_filename.endswith('/core/db.py') and frame.f_lineno in (41,42,43,44):
                    print(json.dumps({'event':'core_db_initialization','line':frame.f_lineno}),file=sys.stderr)
                return trace
            sys.settrace(trace)
    actual=fs.rename_noreplace
    def rename(source,name,destination,planned):
        actual(source,name,destination,planned)
        if (target=='file' and name=='one.jpg') or (target=='frames' and name=='frames') or (target=='root' and name==str(tid)):os._exit(73)
    fs.rename_noreplace=rename
    engine=create_engine('sqlite:///'+str(database),hide_parameters=True)
    try:receipt=stage_retirement(engine,root,tid,{'id':3,'role':'admin','group_id':1,'_auth_kind':'session_jwt'})
    finally:engine.dispose()
    print(json.dumps({'status':receipt.state}))
except Exception as error:
    import traceback
    chain=[];current=error
    while current is not None and len(chain)<6:
        frames=traceback.extract_tb(current.__traceback__)
        original=getattr(current,'orig',current)
        chain.append({'type':type(current).__name__,'sqlite_error':getattr(original,'sqlite_errorname',None),'frames':[{'file':Path(value.filename).name,'line':value.lineno,'function':value.name} for value in frames if 'timelapse' in value.filename]})
        current=current.__context__
    print(json.dumps({'status':'FAIL','error_type':type(error).__name__,'diagnostic_frames':chain}))
    raise SystemExit(2)
