"""Bounded camera read and lifetime-bound exclusive JPEG publication."""
import os
import time
import uuid
from sqlalchemy import text,select,update
from core.timelapse_media_lock import acquire_media_guard
from core.timelapse_manifest import _root
from core.timelapse_publication_schema import bindings
from core.timelapse_ownership_schema import anchors
from modules.archives.timelapse_frame_namespace import bind_or_open_namespace
from modules.archives.ownership import capture_content_owner

MAX_BYTES=16*1024*1024
MAX_PIXELS=4096*4096


def valid_jpeg(payload):
    """Bound declared dimensions before decoding with the existing OpenCV codec."""
    if type(payload) is not bytes or not 4<=len(payload)<=MAX_BYTES or payload[:2]!=b'\xff\xd8' or payload[-2:]!=b'\xff\xd9':return False
    offset=2;dimensions=None
    while offset<len(payload):
        if payload[offset]!=255:return False
        while offset<len(payload) and payload[offset]==255:offset+=1
        if offset>=len(payload):return False
        marker=payload[offset];offset+=1
        if marker==0xDA:break
        if marker in (0,0xD8,0xD9) or 0xD0<=marker<=0xD7:return False
        if marker==1:continue
        if offset+2>len(payload):return False
        size=int.from_bytes(payload[offset:offset+2],'big')
        if size<2 or offset+size>len(payload):return False
        if marker in (0xC0,0xC1,0xC2,0xC3,0xC5,0xC6,0xC7,0xC9,0xCA,0xCB,0xCD,0xCE,0xCF):
            if dimensions is not None or size<8:return False
            height=int.from_bytes(payload[offset+3:offset+5],'big')
            width=int.from_bytes(payload[offset+5:offset+7],'big')
            if not 0<height<=4096 or not 0<width<=4096 or height*width>MAX_PIXELS:return False
            dimensions=(height,width)
        offset+=size
    if dimensions is None:return False
    import cv2,numpy as np
    image=cv2.imdecode(np.frombuffer(payload,dtype=np.uint8),cv2.IMREAD_GRAYSCALE)
    return image is not None and image.shape==dimensions


# Closed set of tables whose exact rows _lock_camera_graph row-locks.
_CAMERA_GRAPH_LOCK_TABLES = frozenset({'print_files', 'education_cost_centers', 'models', 'users'})


def _lock_graph_rows(db, table, identifiers):
    if table not in _CAMERA_GRAPH_LOCK_TABLES:
        raise ValueError('Unsupported capture graph lock target')
    for identifier in sorted(set(identifiers)):
        if db.execute(text(f'SELECT id FROM {table} WHERE id=:id FOR UPDATE NOWAIT'), {'id': identifier}).first() is None:  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- table checked against the _CAMERA_GRAPH_LOCK_TABLES allowlist; values are bound
            raise ValueError('Capture graph authority unavailable')


def _lock_camera_graph(db, job_id):
    """Fence every mutable input to capture_content_owner; caller owns locks.

    Namespace admission already locks job/printer/timelapse. PostgreSQL row
    locks cannot fence new predicate matches, so those two relations require
    short SHARE locks. SQLite's admitted writer transaction fences all DML.
    """
    if db.get_bind().dialect.name != 'postgresql':
        return
    for table in ('education_submissions', 'education_upload_operations'):
        db.execute(text(f'LOCK TABLE {table} IN SHARE MODE NOWAIT'))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- lock helper receives literal table/column/predicate strings only; values are bound
    job = db.execute(text('SELECT print_file_id,model_id FROM jobs WHERE id=:id'), {'id': job_id}).mappings().one()
    if job['print_file_id'] is None:
        db.execute(text('LOCK TABLE print_files IN SHARE MODE NOWAIT'))
        files = db.execute(text('SELECT id FROM print_files WHERE job_id=:id ORDER BY id'), {'id': job_id}).scalars().all()
    else:
        files = [job['print_file_id']]
    if len(files) != 1:
        raise ValueError('Capture file authority unavailable')
    file_id = files[0]
    lock = lambda table, identifiers: _lock_graph_rows(db, table, identifiers)
    # The file lock protects stored_path/model_id before selecting its graph.
    lock('print_files', [file_id])
    education = db.execute(text('SELECT model_id,submitted_by,cost_center_id FROM education_submissions '
        'WHERE job_id=:job OR print_file_id=:file ORDER BY id'), {'job': job_id, 'file': file_id}).mappings().all()
    if len(education) > 1:
        raise ValueError('Capture graph authority ambiguous')
    # Fixed table order, sorted exact row IDs; no camera-time graph reuse.
    lock('education_cost_centers', [row['cost_center_id'] for row in education])
    lock('models', ([job['model_id']] if job['model_id'] is not None else []) + [row['model_id'] for row in education])
    lock('users', [row['submitted_by'] for row in education])


def _camera_authority(db,nonce,printer_id,job_id):
    _lock_camera_graph(db, job_id)
    owner=db.execute(select(anchors).where(anchors.c.operation_id==nonce)).mappings().one()
    printer=db.execute(text('SELECT org_id,shared,is_active,timelapse_enabled FROM printers WHERE id=:id'),{'id':printer_id}).mappings().one()
    def truth(value):
        if type(value) is bool:return value
        if type(value) is int and value in (0,1):return value==1
        raise ValueError('Capture visibility unavailable')
    current=capture_content_owner(db.connection(),job_id)
    from core.tenancy_mode import printer_visible_install_wide
    # Same audience rule as capture creation; an organization created mid-print
    # changes it and stops capture (fail-closed).
    audience=printer_visible_install_wide(db.connection(),printer['org_id'],truth(printer['shared']))
    if (not truth(printer['is_active']) or not truth(printer['timelapse_enabled'])
        or printer['org_id']!=owner['capture_printer_org_id'] or audience!=bool(owner['capture_printer_shared'])
        or current['state']!='known' or current['org_id']!=owner['owner_org_id']
        or current['print_file_id']!=owner['print_file_id'] or current['education_submission_id']!=owner['education_submission_id']):
        raise ValueError('Capture authority changed')


def _capture_authority(db, *, nonce, timelapse_id, printer_id, job_id, allow_ended=False):
    import re
    if type(nonce) is not str or re.fullmatch('[0-9a-f]{32}',nonce) is None or any(type(v) is not int or v<=0 for v in (timelapse_id,printer_id,job_id)):
        raise ValueError('Capture ticket unavailable')
    for table,identifier in (('jobs',job_id),('printers',printer_id),('timelapses',timelapse_id)):
        if db.get_bind().dialect.name=='postgresql':
            if db.execute(text(f'SELECT id FROM {table} WHERE id=:id FOR UPDATE NOWAIT'),{'id':identifier}).first() is None:raise ValueError('Capture authority unavailable')  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- lock helper receives literal table/column/predicate strings only; values are bound
        elif db.execute(text(f'UPDATE {table} SET id=id WHERE id=:id'),{'id':identifier}).rowcount!=1:raise ValueError('Capture authority unavailable')  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- lock helper receives literal table/column/predicate strings only; values are bound
    row=dict(db.execute(text('SELECT printer_id,print_job_id,filename,status,ownership_operation_id,retirement_operation_id FROM timelapses WHERE id=:id'),{'id':timelapse_id}).mappings().one())
    owner=db.execute(select(anchors).where(anchors.c.operation_id==nonce)).mappings().one()
    job=db.execute(text('SELECT printer_id,status FROM jobs WHERE id=:id'),{'id':job_id}).mappings().one()
    if (row['printer_id']!=printer_id or row['print_job_id']!=job_id or row['ownership_operation_id']!=nonce or row['status']!='capturing' or row['retirement_operation_id'] is not None
        or owner['timelapse_id']!=timelapse_id or owner['printer_id']!=printer_id or owner['scheduled_job_id']!=job_id or owner['filename']!=row['filename']
        or owner['owner_state'] not in ('known','shared') or owner['ownership_source']!='first_capture_job_file'
        or job['printer_id']!=printer_id or (not allow_ended and job['status']!='printing')):
        raise ValueError('Capture authority changed')
    _camera_authority(db,nonce,printer_id,job_id)
    return row


def capture_owned_frame(*,sessions,root,http,base,printer_id,timelapse_id,nonce,job_id,auth=None):
    from modules.archives.timelapse_frame_publication import FrameJournal
    directory=None
    try:
        with acquire_media_guard(root,timelapse_id) as guard,_root(root) as (root_fd,identity):
            try:
                def authority(db,*,allow_ended):
                    return _capture_authority(db,nonce=nonce,timelapse_id=timelapse_id,printer_id=printer_id,job_id=job_id,allow_ended=allow_ended)
                journal=FrameJournal(sessions=sessions,root_fd=root_fd,root_identity=identity,guard=guard,authority=authority,
                    nonce=nonce,timelapse_id=timelapse_id,printer_id=printer_id)
                if journal.recover():return True
                with sessions() as db:
                    directory=bind_or_open_namespace(db,root_fd,identity,guard,nonce=nonce,
                        timelapse_id=timelapse_id,printer_id=printer_id,job_id=job_id)
                    _camera_authority(db,nonce,printer_id,job_id)
                    db.commit()
                journal.inventory(directory)
                deadline=time.monotonic()+10
                data=bytearray()
                with http.stream('GET',f'{base}/api/frame.jpeg?src=printer_{printer_id}',timeout=5,follow_redirects=False,headers={'Accept-Encoding':'identity'},**({'auth':auth} if auth else {})) as response:
                    if response.status_code!=200 or response.headers.get('content-encoding','identity').lower() not in ('','identity'):return False
                    for chunk in response.iter_raw(chunk_size=65536):
                        if type(chunk) is not bytes or len(data)+len(chunk)>MAX_BYTES or time.monotonic()>=deadline:return False
                        data.extend(chunk)
                payload=bytes(data)
                if time.monotonic()>=deadline or not valid_jpeg(payload) or time.monotonic()>=deadline:return False
                return journal.write(journal.admit(payload),payload)
            finally:
                if directory is not None:
                    # Retire ownership before close: an exception leaves the FD
                    # number uncertain, so neither retry it nor report success.
                    descriptor,directory=directory,None
                    os.close(descriptor)
    except Exception:
        return False
