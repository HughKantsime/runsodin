"""Owned PG/real directory probe; no camera or frame publication."""
import os
import tempfile
import time
from pathlib import Path
from sqlalchemy import create_engine,text
from sqlalchemy.orm import Session
from core.timelapse_ownership_schema import anchors
from core.timelapse_publication_schema import lifetimes,versions,namespaces,bindings
from core.timelapse_media_lock import acquire_media_guard
from core.timelapse_manifest import _root
from modules.archives.timelapse_frame_namespace import bind_or_open_namespace,FrameNamespaceUnavailable


def main():
    assert os.getuid()==10001
    engine=create_engine(os.environ['FRAME_NAMESPACE_TEST_URL'])
    nonce='a'*32
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE jobs(id INTEGER PRIMARY KEY,printer_id INTEGER,status TEXT)'))
        conn.execute(text('CREATE TABLE printers(id INTEGER PRIMARY KEY)'))
        conn.execute(text('CREATE TABLE timelapses(id INTEGER PRIMARY KEY,printer_id INTEGER,print_job_id INTEGER,filename TEXT,status TEXT,ownership_operation_id TEXT,retirement_operation_id TEXT)'))
        for table in (anchors,lifetimes,versions,namespaces,bindings):table.create(conn)
        conn.execute(text("INSERT INTO jobs VALUES(1,2,'printing')"))
        conn.execute(text('INSERT INTO printers VALUES(2)'))
        conn.execute(text("INSERT INTO timelapses VALUES(3,2,1,'printer_2/owned.mp4','capturing',:nonce,NULL)"),{'nonce':nonce})
        conn.execute(anchors.insert().values(operation_id=nonce,timelapse_id=3,printer_id=2,scheduled_job_id=1,
            filename='printer_2/owned.mp4',owner_state='known',owner_org_id=4,ownership_source='first_capture_job_file',
            print_file_id=5,capture_printer_org_id=4,capture_printer_shared=False))
    with tempfile.TemporaryDirectory(prefix='namespace-proof-') as folder:
        root=Path(folder)
        def open_bound(db):
            with acquire_media_guard(root,3) as guard,_root(root) as (fd,identity):
                owned=bind_or_open_namespace(db,fd,identity,guard,nonce=nonce,timelapse_id=3,printer_id=2,job_id=1)
                try:
                    inode=os.fstat(owned).st_ino
                    db.commit();guard.assert_owned()
                    return inode
                finally:os.close(owned)
        with Session(engine) as db:first=open_bound(db)
        with Session(engine) as db:assert open_bound(db)==first
        for table,identifier in (('jobs',1),('printers',2),('timelapses',3)):
            with engine.connect() as blocker:
                transaction=blocker.begin()
                blocker.execute(text(f'SELECT id FROM {table} WHERE id=:id FOR UPDATE'),{'id':identifier})
                with Session(engine) as db,acquire_media_guard(root,3) as guard,_root(root) as (fd,identity):
                    started=time.monotonic()
                    try:bind_or_open_namespace(db,fd,identity,guard,nonce=nonce,timelapse_id=3,printer_id=2,job_id=1)
                    except FrameNamespaceUnavailable as refusal:
                        assert getattr(getattr(refusal.__context__,'orig',None),'sqlstate',None)=='55P03'
                    else:raise AssertionError('Contended lock accepted')
                    assert time.monotonic()-started<2
                    assert db.in_transaction()
                    db.rollback();guard.assert_owned()
                transaction.rollback()
            with Session(engine) as db:assert open_bound(db)==first
    engine.dispose()
    print('frame-namespace-native-pass: committed reopen and three NOWAIT contention/caller-rollback cases')


if __name__=='__main__':main()
