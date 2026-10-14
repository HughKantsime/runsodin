"""Platform maintenance of unknown-owner legacy frames, not tenant provenance."""
import hashlib
import io
import json
import math
import os
import re
import stat
import zipfile
from PIL import Image
from fastapi import HTTPException
from sqlalchemy import update
from modules.vision.models import VisionDetection

VISION_FRAMES_DIR='/data/vision_frames'
MAX_IMAGE_BYTES=8*1024*1024
MAX_PIXELS=4096*4096
MAX_EXPORT_BYTES=64*1024*1024
MAX_ROWS=500
CLASSES={'spaghetti':0,'first_layer':1,'detachment':2,'build_plate_empty':3}


def maintenance(user):
    return bool(user and user.get('role')=='admin' and not user.get('group_id'))


def metadata(raw):
    if raw is None:return {}
    if not isinstance(raw,str) or len(raw)>65536:raise ValueError('metadata')
    try:value=json.loads(raw)
    except (ValueError,RecursionError):raise ValueError('metadata') from None
    if not isinstance(value,dict):raise ValueError('metadata')
    return value


def snapshot(printer_id,relative):
    if isinstance(printer_id,bool) or not isinstance(printer_id,int) or printer_id<=0:raise ValueError('path')
    if not isinstance(relative,str):raise ValueError('path')
    parts=relative.split('/')
    # <printer>/<frame>.jpg for detections, <printer>/training/<frame>.jpg for collected samples.
    directories,name=parts[:-1],parts[-1]
    if (directories not in ([str(printer_id)],[str(printer_id),'training'])
            or not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,199}',name)):raise ValueError('path')
    fds=[]
    try:
        flags=os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK
        root=os.open(VISION_FRAMES_DIR,flags|os.O_DIRECTORY);fds.append(root)
        directory=root
        for part in directories:
            directory=os.open(part,flags|os.O_DIRECTORY,dir_fd=directory);fds.append(directory)
        fd=os.open(name,flags,dir_fd=directory);fds.append(fd)
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0<info.st_size<=MAX_IMAGE_BYTES:raise ValueError('size')
        with os.fdopen(os.dup(fd),'rb') as stream:data=stream.read(MAX_IMAGE_BYTES+1)
        if not 0<len(data)<=MAX_IMAGE_BYTES:raise ValueError('size')
        with Image.open(io.BytesIO(data)) as image:
            width,height=image.size
            if image.format!='JPEG' or width<=0 or height<=0 or width*height>MAX_PIXELS:raise ValueError('image')
            image.load()
        return data,width,height,hashlib.sha256(data).hexdigest()
    except Image.DecompressionBombError:raise ValueError('image') from None
    finally:
        for fd in reversed(fds):os.close(fd)


def geometry(label,bbox,width,height):
    label='first_layer' if label=='first_layer_failure' else label
    if label=='false_positive':
        if bbox!=[]:raise ValueError('negative geometry')
        return label,True,[]
    if not isinstance(label,str) or label not in CLASSES:raise ValueError('class')
    if not isinstance(bbox,list) or len(bbox)!=4 or any(isinstance(x,bool) or not isinstance(x,(int,float)) or abs(x)>4096 or not math.isfinite(x) for x in bbox):raise ValueError('geometry')
    x1,y1,x2,y2=bbox
    if not (0<=x1<x2<=width and 0<=y1<y2<=height):raise ValueError('geometry')
    return label,False,bbox


def annotation(value,width,height,digest):
    if not isinstance(value,dict) or type(value.get('version')) is not int or value.get('version')!=1:raise ValueError('annotation')
    if value.get('maintenance_only') is not True:raise ValueError('annotation provenance')
    revision=value.get('revision')
    if isinstance(revision,bool) or not isinstance(revision,int) or revision<1:raise ValueError('revision')
    if value.get('image_sha256')!=digest:raise ValueError('changed image')
    label,negative,bbox=geometry(value.get('class'),value.get('bbox'),width,height)
    if value.get('negative') is not negative:raise ValueError('negative')
    return label,negative,bbox


def versioned_annotation(raw):
    """Only bounded bytes reach JSON parsing; the v1 maintenance API is unchanged."""
    if len(raw)>4096:raise HTTPException(413,'Annotation request exceeds 4 KiB')
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('duplicate')
            result[key]=value
        return result
    try:
        body=json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite')))
        if not isinstance(body,dict) or set(body)!={'expected_image_sha256','expected_revision','class','negative','bbox'}:raise ValueError('fields')
        digest=body['expected_image_sha256'];revision=body['expected_revision'];label=body['class']
        if not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest):raise ValueError('digest')
        if type(revision) is not int or not 0<=revision<=2147483646:raise ValueError('revision')
        if type(label) is not str or label not in {*CLASSES,'false_positive'}:raise ValueError('class')
        if type(body['negative']) is not bool or body['negative']!=(label=='false_positive'):raise ValueError('negative')
        geometry(label,body['bbox'],4096,4096)
        return body
    except (UnicodeError,ValueError,TypeError,RecursionError):raise HTTPException(400,'Invalid versioned annotation') from None

async def read_annotation_body(request):
    declared=request.headers.get('content-length')
    if declared is not None:
        try:
            if int(declared)>4096:raise HTTPException(413,'Annotation request exceeds 4 KiB')
        except ValueError:raise HTTPException(400,'Invalid annotation size') from None
    chunks=[];size=0
    async for chunk in request.stream():
        size+=len(chunk)
        if size>4096:raise HTTPException(413,'Annotation request exceeds 4 KiB')
        chunks.append(chunk)
    return b''.join(chunks)


def label_frame(db,identity,body,user,*,expected_digest=None):
    if not maintenance(user):raise HTTPException(404,'Detection not found')
    if not isinstance(body,dict):raise HTTPException(400,'Invalid annotation')
    expected=body.get('expected_revision')
    if isinstance(expected,bool) or not isinstance(expected,int) or expected<0:raise HTTPException(400,'expected_revision is required')
    # Reject malformed input before touching an image or database state.
    try:geometry(body.get('class'),body.get('bbox'),4096,4096)
    except (ValueError,TypeError):raise HTTPException(400,'Invalid annotation geometry or class') from None
    row=db.query(VisionDetection).filter(VisionDetection.id==identity).first()
    if row is None:raise HTTPException(404,'Detection not found')
    old=row.metadata_json
    captured_printer,captured_path=row.printer_id,row.frame_path
    try:
        value=metadata(old)
        previous=value.get('training_annotation')
        actual=0 if previous is None else previous.get('revision') if isinstance(previous,dict) else None
        if isinstance(actual,bool) or not isinstance(actual,int) or actual<0:raise ValueError('revision')
        data,width,height,digest=snapshot(captured_printer,captured_path)
        if expected_digest is not None and digest!=expected_digest:
            raise HTTPException(409,'Frame changed; refresh the preview before editing')
        if previous is not None:annotation(previous,width,height,digest)
        label,negative,bbox=geometry(body.get('class'),body.get('bbox'),width,height)
    except (ValueError,TypeError,OSError,Image.DecompressionBombError):raise HTTPException(400,'Frame or existing annotation is invalid') from None
    if actual!=expected:raise HTTPException(409,'Annotation changed; refresh before editing')
    value['training_annotation']={'version':1,'revision':actual+1,'class':label,'negative':negative,'bbox':bbox,'image_sha256':digest,'maintenance_only':True}
    encoded=json.dumps(value,allow_nan=False)
    if len(encoded)>65536:raise HTTPException(400,'Annotation metadata is too large')
    query=update(VisionDetection).where(VisionDetection.id==identity,VisionDetection.printer_id==captured_printer,VisionDetection.frame_path==captured_path,
        VisionDetection.metadata_json.is_(None) if old is None else VisionDetection.metadata_json==old).values(metadata_json=encoded)
    try:
        changed=db.execute(query.execution_options(synchronize_session=False)).rowcount
        if changed!=1:
            db.rollback();raise HTTPException(409,'Annotation changed; refresh before editing')
        db.commit()
    except HTTPException:raise
    except Exception:
        try:db.rollback()
        except Exception:pass
        raise HTTPException(503,'Annotation persistence unconfirmed; refresh before retrying') from None
    return {'id':identity,'labeled':True,'annotation_revision':actual+1}


def export_frames(db,user):
    rows=[] if not maintenance(user) else db.query(VisionDetection).filter(VisionDetection.frame_path.isnot(None)).order_by(VisionDetection.id).limit(MAX_ROWS+1).all()
    if len(rows)>MAX_ROWS:raise HTTPException(413,'Dataset exceeds bounded export limit')
    result=io.BytesIO();excluded=[];count=0;total=0
    with zipfile.ZipFile(result,'w',zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('data.yaml','names:\n  0: spaghetti\n  1: first_layer\n  2: detachment\n  3: build_plate_empty\nnc: 4\ntrain: images/\nval: images/\n')
        for row in rows:
            try:
                value=metadata(row.metadata_json).get('training_annotation')
                if value is None:excluded.append({'id':row.id,'reason':'unannotated'});continue
                data,width,height,digest=snapshot(row.printer_id,row.frame_path)
                label,negative,bbox=annotation(value,width,height,digest)
            except (ValueError,TypeError,OSError,Image.DecompressionBombError):
                excluded.append({'id':row.id,'reason':'invalid_annotation_or_frame'});continue
            total+=len(data)
            if total>MAX_EXPORT_BYTES:raise HTTPException(413,'Dataset exceeds bounded export byte limit')
            base=f'detection_{row.id}'
            archive.writestr(f'images/{base}.jpg',data)
            text=''
            if not negative:
                x1,y1,x2,y2=bbox
                text=f'{CLASSES[label]} {(x1+x2)/2/width:.6f} {(y1+y2)/2/height:.6f} {(x2-x1)/width:.6f} {(y2-y1)/height:.6f}\n'
            archive.writestr(f'labels/{base}.txt',text);count+=1
        archive.writestr('export-report.json',json.dumps({'exported':count,'excluded':excluded,'ownership':'unknown historical owner; platform maintenance annotations only'}))
    result.seek(0);return result


def list_frames(db,user,printer_id,labeled,limit,offset):
    if not maintenance(user):return []
    if isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=200 or isinstance(offset,bool) or not isinstance(offset,int) or offset<0:raise HTTPException(400,'Invalid pagination')
    query=db.query(VisionDetection).filter(VisionDetection.frame_path.isnot(None))
    if printer_id is not None:query=query.filter(VisionDetection.printer_id==printer_id)
    rows=query.order_by(VisionDetection.created_at.desc(),VisionDetection.id.desc()).limit(5001).all()
    if len(rows)>5000:raise HTTPException(413,'Dataset exceeds bounded listing limit')
    result=[]
    for row in rows:
        try:
            value=metadata(row.metadata_json).get('training_annotation')
            is_labeled=isinstance(value,dict) and type(value.get('version')) is int and value.get('version')==1
        except (ValueError,TypeError):value=None;is_labeled=False
        if labeled is not None and is_labeled!=labeled:continue
        result.append({'id':row.id,'printer_id':row.printer_id,'detection_type':row.detection_type,'confidence':row.confidence,'status':row.status,'frame_path':row.frame_path,'bbox_json':row.bbox_json,'created_at':row.created_at.isoformat() if row.created_at else None,'annotation':value if is_labeled else None,'ownership':'unknown','maintenance_only':True})
    return result[offset:offset+limit]
