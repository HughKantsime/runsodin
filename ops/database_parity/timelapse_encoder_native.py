"""Owned Linux SQLite/PostgreSQL real frame-journal -> encode -> MP4 proof."""
import os
import json
import tempfile
from pathlib import Path
from contextlib import contextmanager
from types import SimpleNamespace
import cv2,numpy as np
import subprocess
from sqlalchemy import create_engine,text,select,func
from sqlalchemy.orm import Session
from core.timelapse_ownership_schema import anchors
from core.timelapse_publication_schema import lifetimes,versions,namespaces,bindings,publications,facts
from core.models import AuditLog
from core.timelapse_media_lock import acquire_media_guard
from core.timelapse_manifest import _root
from modules.archives.timelapse_frame_capture import capture_owned_frame,_capture_authority
from modules.archives.timelapse_frame_publication import FrameJournal
from modules.archives import timelapse_encode as encode


def verify(url):
    engine=create_engine(url);sessions=lambda:Session(engine)
    nonce='a'*32
    with engine.begin() as db:
        statements=[
            'CREATE TABLE jobs(id INTEGER PRIMARY KEY,printer_id INTEGER,status TEXT,print_file_id INTEGER,model_id INTEGER,charged_to_org_id INTEGER,submitted_by INTEGER)',
            'CREATE TABLE printers(id INTEGER PRIMARY KEY,org_id INTEGER,shared BOOLEAN,is_active BOOLEAN,timelapse_enabled BOOLEAN)',
            'CREATE TABLE timelapses(id INTEGER PRIMARY KEY,printer_id INTEGER,print_job_id INTEGER,filename TEXT,status TEXT,ownership_operation_id TEXT,retirement_operation_id TEXT,frame_count INTEGER,duration_seconds FLOAT,file_size_mb FLOAT,completed_at TIMESTAMP)',
            'CREATE TABLE print_files(id INTEGER PRIMARY KEY,org_id INTEGER,model_id INTEGER,stored_path TEXT,job_id INTEGER,created_by INTEGER)',
            'CREATE TABLE models(id INTEGER PRIMARY KEY,org_id INTEGER,print_file_id INTEGER)',
            'CREATE TABLE education_submissions(id INTEGER PRIMARY KEY,org_id INTEGER,job_id INTEGER,print_file_id INTEGER,model_id INTEGER,operation_id TEXT,submitted_by INTEGER,cost_center_id INTEGER)',
            'CREATE TABLE education_upload_operations(operation_id TEXT PRIMARY KEY,org_id INTEGER,user_id INTEGER,state TEXT,final_path TEXT)',
            'CREATE TABLE users(id INTEGER PRIMARY KEY,group_id INTEGER)',
            'CREATE TABLE education_cost_centers(id INTEGER PRIMARY KEY,org_id INTEGER)',
        ]
        for statement in statements:db.execute(text(statement))
        for table in (anchors,lifetimes,versions,namespaces,bindings,publications,facts,AuditLog.__table__):table.create(db)
        db.execute(text("INSERT INTO jobs VALUES(1,2,'printing',5,6,4,7)"))
        db.execute(text('INSERT INTO printers VALUES(2,4,FALSE,TRUE,TRUE)'))
        db.execute(text("INSERT INTO timelapses VALUES(3,2,1,'printer_2/owned.mp4','capturing',:nonce,NULL,0,NULL,NULL,NULL)"),{'nonce':nonce})
        db.execute(text("INSERT INTO print_files VALUES(5,4,6,'/owned/file',1,7)"))
        db.execute(text('INSERT INTO models VALUES(6,4,5)'));db.execute(text('INSERT INTO users VALUES(7,4)'))
        db.execute(text('INSERT INTO education_cost_centers VALUES(8,4)'))
        db.execute(anchors.insert().values(operation_id=nonce,timelapse_id=3,printer_id=2,scheduled_job_id=1,
            filename='printer_2/owned.mp4',owner_state='known',owner_org_id=4,ownership_source='first_capture_job_file',
            print_file_id=5,capture_printer_org_id=4,capture_printer_shared=False))
    checked=[]
    with tempfile.TemporaryDirectory(prefix='owned-encoder-') as folder:
        root=Path(folder);root.chmod(0o700)
        current=[None]
        @contextmanager
        def stream(*args,**kwargs):yield SimpleNamespace(status_code=200,headers={},iter_raw=lambda **kw:[current[0]])
        next_number=1
        for number,bgr in ((1,(0,0,255)),(3,(0,255,0)),(10,(255,0,0))):
            while next_number<number:
                with acquire_media_guard(root,3) as guard,_root(root) as (fd,identity):
                    def authority(db,*,allow_ended):return _capture_authority(db,nonce=nonce,timelapse_id=3,printer_id=2,job_id=1,allow_ended=allow_ended)
                    FrameJournal(sessions=sessions,root_fd=fd,root_identity=identity,guard=guard,authority=authority,
                        nonce=nonce,timelapse_id=3,printer_id=2).admit(b'reserved frame number')
                next_number+=1
            if number==10:
                with engine.begin() as db:db.execute(text("UPDATE jobs SET status='paused' WHERE id=1"))
                prior_namespace=next(root.glob('frames_*')).stat().st_ino
                assert not encode.encode_owned_timelapse(sessions=sessions,root=root,timelapse_id=3,printer_id=2,nonce=nonce,job_id=1)
                assert not list(root.glob('encode_*.mp4'))
                with sessions() as db:
                    assert db.execute(text('SELECT status FROM timelapses WHERE id=3')).scalar_one()=='capturing'
                    assert db.execute(select(func.count()).select_from(lifetimes)).scalar_one()==1
                    assert db.execute(select(publications.c.operation_id).where(publications.c.kind=='encode')).first() is None
                with engine.begin() as db:db.execute(text("UPDATE jobs SET status='printing' WHERE id=1"))
                assert next(root.glob('frames_*')).stat().st_ino==prior_namespace
                checked.append('actual-pause-resume-same-lifetime')
            image=np.empty((32,32,3),dtype=np.uint8);image[:]=bgr
            valid,jpeg=cv2.imencode('.jpg',image);assert valid;current[0]=jpeg.tobytes()
            assert capture_owned_frame(sessions=sessions,root=root,http=SimpleNamespace(stream=stream),base='synthetic',printer_id=2,timelapse_id=3,nonce=nonce,job_id=1)
            next_number+=1
        originals={str(path):(path.stat().st_ino,path.read_bytes()) for path in root.glob('frames_*/*.jpg')}
        with engine.begin() as db:db.execute(text("UPDATE jobs SET status='completed' WHERE id=1"))
        original_complete=encode.complete_media_transport;original_rename=encode.rename_noreplace;original_commit=Session.commit
        ack=[]
        def unlocked(*args,**kwargs):
            # Another actual connection can write while ffmpeg runs; no DB lock survives admission.
            with engine.begin() as db:db.execute(text('UPDATE print_files SET stored_path=stored_path WHERE id=5'))
            checked.append('codec-db-closed');return original_complete(*args,**kwargs)
        def guarded_rename(*args):
            if engine.dialect.name=='postgresql':
                for mutation in ('UPDATE print_files SET org_id=9 WHERE id=5',"UPDATE jobs SET status='printing' WHERE id=1",'UPDATE printers SET org_id=9 WHERE id=2',"UPDATE timelapses SET filename='foreign.mp4' WHERE id=3",'UPDATE models SET org_id=9 WHERE id=6',"INSERT INTO education_submissions VALUES(10,4,1,5,6,'new',7,8)"):
                    with engine.connect() as competitor:
                        competitor.execute(text("SET LOCAL lock_timeout='100ms'"))
                        try:competitor.execute(text(mutation))
                        except Exception as error:assert getattr(error.orig,'sqlstate',None)=='55P03'
                        else:raise AssertionError('Encode ownership mutation escaped final locks')
                        competitor.rollback()
                checked.append('six-native-authority-fences')
            return original_rename(*args)
        def commit_ack(db):
            phase=db.execute(select(publications.c.phase).where(publications.c.kind=='encode')).scalar_one_or_none()
            original_commit(db)
            if phase=='complete' and not ack:ack.append(True);raise RuntimeError('Completed commit ACK lost')
        encode.complete_media_transport=unlocked;encode.rename_noreplace=guarded_rename;Session.commit=commit_ack
        try:
            assert encode.encode_owned_timelapse(sessions=sessions,root=root,timelapse_id=3,printer_id=2,nonce=nonce,job_id=1)
        finally:encode.complete_media_transport=original_complete;encode.rename_noreplace=original_rename;Session.commit=original_commit
        assert ack==[True]
        with sessions() as db:
            row=db.execute(text('SELECT * FROM timelapses WHERE id=3')).mappings().one()
            op=db.execute(select(publications).where(publications.c.kind=='encode')).mappings().one()
            assert row['status']=='ready' and row['ownership_operation_id']==op['result_nonce'] and row['filename']==op['destination_path']
            assert op['phase']=='complete' and row['frame_count']==3 and row['duration_seconds']==.1
            assert db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='timelapse_encode_complete'")).scalar_one()==1
            output=root/row['filename'];operation=op['operation_id'];published_inode=output.stat().st_ino
        decoded=subprocess.check_output(['/usr/bin/ffmpeg','-nostdin','-v','error','-i',str(output),'-f','rawvideo','-pix_fmt','rgb24','-'])
        size=32*32*3;assert len(decoded)==size*3
        dominant=[]
        for start in range(0,len(decoded),size):
            pixels=decoded[start:start+size];dominant.append(max(range(3),key=lambda c:sum(pixels[c::3])))
        assert dominant==[0,1,2];data=output.read_bytes();assert data.index(b'moov')<data.index(b'mdat')
        assert {str(path):(path.stat().st_ino,path.read_bytes()) for path in root.glob('frames_*/*.jpg')}==originals
        assert encode.encode_owned_timelapse(sessions=sessions,root=root,timelapse_id=3,printer_id=2,nonce=nonce,job_id=1)
        assert output.stat().st_ino==published_inode
        with sessions() as db:assert db.execute(select(publications.c.operation_id).where(publications.c.kind=='encode')).scalars().all()==[operation]
        with engine.begin() as db:db.execute(text('UPDATE print_files SET org_id=9 WHERE id=5'))
        assert not encode.encode_owned_timelapse(sessions=sessions,root=root,timelapse_id=3,printer_id=2,nonce=nonce,job_id=1)
        checked.extend(['real-frame-receipts-gap1-3-10','actual-ordered-full-decode-faststart-originals','complete-ack-exact-result-once','current-revocation-refuses-replay'])
    engine.dispose()
    return {'database':engine.dialect.name,'checks':checked,'owned_media_cleanup':not root.exists()}


if __name__=='__main__':
    with tempfile.TemporaryDirectory(prefix='owned-encoder-sqlite-') as folder:
        sqlite=verify('sqlite:///'+str(Path(folder)/'owned.db'))
    values=[sqlite]
    if os.getenv('ENCODER_TEST_URL'):values.append(verify(os.environ['ENCODER_TEST_URL']))
    print(json.dumps({'status':'PASS','groups':values,'scope':'actual reviewed capture/frame-journal/typed-worker/encode-publication; synthetic cameras, no hardware/full-bootstrap claim'}))
