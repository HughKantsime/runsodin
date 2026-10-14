"""Owned PostgreSQL capture graph contention proof; synthetic camera only."""
import os
import tempfile
from pathlib import Path
from contextlib import contextmanager
from types import SimpleNamespace
from sqlalchemy import create_engine,text
from sqlalchemy.orm import Session
from core.timelapse_ownership_schema import anchors
from core.timelapse_publication_schema import lifetimes,versions,namespaces,bindings,publications,facts
from modules.archives import timelapse_frame_capture as capture
from modules.archives import timelapse_frame_publication as journal


def main():
    import cv2,numpy as np
    engine=create_engine(os.environ['FRAME_NAMESPACE_TEST_URL'])
    nonce='a'*32
    with engine.begin() as db:
        statements=[
            'CREATE TABLE jobs(id INTEGER PRIMARY KEY,printer_id INTEGER,status TEXT,print_file_id INTEGER,model_id INTEGER,charged_to_org_id INTEGER,submitted_by INTEGER)',
            'CREATE TABLE printers(id INTEGER PRIMARY KEY,org_id INTEGER,shared BOOLEAN,is_active BOOLEAN,timelapse_enabled BOOLEAN)',
            'CREATE TABLE timelapses(id INTEGER PRIMARY KEY,printer_id INTEGER,print_job_id INTEGER,filename TEXT,status TEXT,ownership_operation_id TEXT,retirement_operation_id TEXT,frame_count INTEGER)',
            'CREATE TABLE print_files(id INTEGER PRIMARY KEY,org_id INTEGER,model_id INTEGER,stored_path TEXT,job_id INTEGER,created_by INTEGER)',
            'CREATE TABLE models(id INTEGER PRIMARY KEY,org_id INTEGER,print_file_id INTEGER)',
            'CREATE TABLE education_submissions(id INTEGER PRIMARY KEY,org_id INTEGER,job_id INTEGER,print_file_id INTEGER,model_id INTEGER,operation_id TEXT,submitted_by INTEGER,cost_center_id INTEGER)',
            'CREATE TABLE education_upload_operations(operation_id TEXT PRIMARY KEY,org_id INTEGER,user_id INTEGER,state TEXT,final_path TEXT)',
            'CREATE TABLE users(id INTEGER PRIMARY KEY,group_id INTEGER)',
            'CREATE TABLE education_cost_centers(id INTEGER PRIMARY KEY,org_id INTEGER)',
        ]
        for statement in statements:db.execute(text(statement))
        for table in (anchors,lifetimes,versions,namespaces,bindings,publications,facts):table.create(db)
        db.execute(text("INSERT INTO jobs VALUES(1,2,'printing',5,6,4,7)"))
        db.execute(text('INSERT INTO printers VALUES(2,4,FALSE,TRUE,TRUE)'))
        db.execute(text("INSERT INTO timelapses VALUES(3,2,1,'printer_2/owned.mp4','capturing',:nonce,NULL,0)"),{'nonce':nonce})
        db.execute(text("INSERT INTO print_files VALUES(5,4,6,'/owned/file',1,7)"))
        db.execute(text('INSERT INTO models VALUES(6,4,5)'))
        db.execute(text('INSERT INTO users VALUES(7,4)'))
        db.execute(text('INSERT INTO education_cost_centers VALUES(8,4)'))
        db.execute(anchors.insert().values(operation_id=nonce,timelapse_id=3,printer_id=2,scheduled_job_id=1,
            filename='printer_2/owned.mp4',owner_state='known',owner_org_id=4,ownership_source='first_capture_job_file',
            print_file_id=5,capture_printer_org_id=4,capture_printer_shared=False))
    valid,jpeg=cv2.imencode('.jpg',np.zeros((20,20,3),dtype=np.uint8));assert valid
    mutations=["UPDATE print_files SET stored_path='/changed' WHERE id=5",'UPDATE print_files SET model_id=NULL WHERE id=5',
               'UPDATE models SET org_id=9 WHERE id=6',
               "INSERT INTO education_submissions VALUES(10,4,1,5,6,'new',7,8)",
               "INSERT INTO education_upload_operations VALUES('new',4,7,'committed','/owned/file')"]
    with tempfile.TemporaryDirectory(prefix='capture-graph-') as folder:
        root=Path(folder)
        original_rename=journal.rename_noreplace
        checked=[]
        def guarded_rename(*args):
            for mutation in mutations:
                with engine.connect() as competitor:
                    competitor.execute(text("SET LOCAL lock_timeout='100ms'"))
                    try:competitor.execute(text(mutation))
                    except Exception as exc:assert getattr(exc.orig,'sqlstate',None)=='55P03'
                    else:raise AssertionError('Ownership mutation escaped final capture locks')
                    competitor.rollback()
            checked.append(True)
            return original_rename(*args)
        @contextmanager
        def stream(*args,**kwargs):
            # No graph locks span camera transport.
            with engine.begin() as db:db.execute(text('UPDATE print_files SET stored_path=stored_path WHERE id=5'))
            yield SimpleNamespace(status_code=200,headers={},iter_raw=lambda **kw:[jpeg.tobytes()])
        journal.rename_noreplace=guarded_rename
        try:
            assert capture.capture_owned_frame(sessions=lambda:Session(engine),root=root,http=SimpleNamespace(stream=stream),base='synthetic',printer_id=2,timelapse_id=3,nonce=nonce,job_id=1)
        finally:journal.rename_noreplace=original_rename
        assert checked and len(list(root.glob('frames_*/*.jpg')))==1
        with engine.begin() as db:
            for mutation in mutations:db.execute(text(mutation))
        # Real writer contention refuses before entering transport, then rollback
        # releases the competing transaction without adopting any changed graph.
        camera_calls=[]
        @contextmanager
        def forbidden_camera(*args, **kwargs):
            camera_calls.append(True)
            yield SimpleNamespace(status_code=200,headers={},iter_raw=lambda **kw:[jpeg.tobytes()])
        with engine.connect() as blocker:
            blocker.execute(text('UPDATE print_files SET stored_path=stored_path WHERE id=5'))
            assert not capture.capture_owned_frame(sessions=lambda:Session(engine),root=root,http=SimpleNamespace(stream=forbidden_camera),base='synthetic',printer_id=2,timelapse_id=3,nonce=nonce,job_id=1)
            blocker.rollback()
        assert camera_calls==[]
        # Changed graph now refuses before transport; no new frame is published.
        assert not capture.capture_owned_frame(sessions=lambda:Session(engine),root=root,http=SimpleNamespace(stream=stream),base='synthetic',printer_id=2,timelapse_id=3,nonce=nonce,job_id=1)
        assert len(list(root.glob('frames_*/*.jpg')))==1
        with engine.begin() as db:
            db.execute(text("UPDATE print_files SET stored_path='/owned/file',model_id=6 WHERE id=5"))
            db.execute(text('UPDATE models SET org_id=4 WHERE id=6'))
            db.execute(text("UPDATE timelapse_ownership_anchors SET education_submission_id=10 WHERE operation_id=:nonce"), {'nonce': nonce})
        mutations.extend(['UPDATE users SET group_id=9 WHERE id=7', 'UPDATE education_cost_centers SET org_id=9 WHERE id=8'])
        journal.rename_noreplace=guarded_rename
        try:
            assert capture.capture_owned_frame(sessions=lambda:Session(engine),root=root,http=SimpleNamespace(stream=stream),base='synthetic',printer_id=2,timelapse_id=3,nonce=nonce,job_id=1)
        finally:journal.rename_noreplace=original_rename
        assert len(list(root.glob('frames_*/*.jpg')))==2
        # Fallback job-to-file predicate requires a table fence against insertion.
        with engine.begin() as db:
            db.execute(text('DELETE FROM education_submissions'))
            db.execute(text('DELETE FROM education_upload_operations'))
            db.execute(text('UPDATE jobs SET print_file_id=NULL WHERE id=1'))
            db.execute(text("UPDATE timelapse_ownership_anchors SET education_submission_id=NULL WHERE operation_id=:nonce"), {'nonce': nonce})
        mutations[:]=["INSERT INTO print_files VALUES(15,4,6,'/other',1,7)"]
        journal.rename_noreplace=guarded_rename
        try:
            assert capture.capture_owned_frame(sessions=lambda:Session(engine),root=root,http=SimpleNamespace(stream=stream),base='synthetic',printer_id=2,timelapse_id=3,nonce=nonce,job_id=1)
        finally:journal.rename_noreplace=original_rename
        with engine.begin() as db:db.execute(text(mutations[0]))
        assert len(list(root.glob('frames_*/*.jpg')))==3
    engine.dispose()
    print('capture-graph-native-pass: five graph mutations blocked during actual no-replace publish, allowed after commit; camera unlocked; drift refused')

if __name__=='__main__':main()
