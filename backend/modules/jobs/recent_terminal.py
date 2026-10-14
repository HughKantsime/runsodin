"""Bounded recent terminal projection with file/tenant/Education audience proof."""
import base64
import re
from datetime import datetime,timezone
from io import BytesIO
import warnings
from PIL import Image
from fastapi import APIRouter,Depends,Query,HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session
from core.db import get_db
from core.rbac import require_role,_get_org_filter,should_filter_org,check_org_access
from core.api_token_policy import token_scope_granted
from core.registry import registry
from core.model_preview_access import is_education_owned_file

MAX_THUMBNAIL_BYTES=262144


class RecentTerminalJob(BaseModel):
    id:int
    item_name:str
    status:str
    actual_end:str|None
    duration_seconds:float|None
    printer_name:str|None
    model_name:str|None
    fail_reason:str|None
    thumbnail_data:str|None


def utc_time(value):
    # Actual-time writers use UTC; timezone=False storage drops ORM timezone.
    if isinstance(value,datetime):parsed=value
    elif type(value) is str and re.fullmatch(r'\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?',value):
        try:parsed=datetime.fromisoformat(value)
        except ValueError:return None
    else:return None
    if parsed.tzinfo is None:parsed=parsed.replace(tzinfo=timezone.utc)
    try:return parsed.astimezone(timezone.utc)
    except (ValueError,OverflowError):return None


def _time_key(value):
    parsed=utc_time(value)
    return parsed.isoformat(timespec='microseconds') if parsed is not None else None


def thumbnail_data(value):
    if type(value) is not str or not 1<=len(value)<=4*((MAX_THUMBNAIL_BYTES+2)//3):return None
    try:
        content=base64.b64decode(value,validate=True)
        if not 1<=len(content)<=MAX_THUMBNAIL_BYTES:return None
        with warnings.catch_warnings():
            warnings.simplefilter('error',Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                kind={'PNG':'png','JPEG':'jpeg'}.get(image.format)
                if kind is None or not 1<=image.width<=1024 or not 1<=image.height<=1024 or image.width*image.height>1048576 or getattr(image,'n_frames',1)!=1:return None
                image.verify()
            with Image.open(BytesIO(content)) as image:image.load()
        return f'data:image/{kind};base64,'+base64.b64encode(content).decode('ascii')
    except (ValueError,OSError,Image.DecompressionBombError,Image.DecompressionBombWarning):return None


def _scope(user,name):
    scopes=user.get('_token_scopes',[])
    return not (scopes or user.get('_auth_kind')=='user_api_token') or token_scope_granted(scopes,name)


# Broad persisted association evidence is protective even without policy service.
_SUBMISSION_LINKS="""raw_s.job_id=a.job_id OR raw_s.print_file_id=a.print_file_id OR raw_s.model_id=a.model_id
 OR EXISTS (SELECT 1 FROM print_files associated JOIN print_files original ON original.id=raw_s.print_file_id
 WHERE (associated.id=a.print_file_id OR associated.job_id=a.job_id OR associated.model_id=a.model_id
 OR associated.id=(SELECT print_file_id FROM models WHERE id=a.model_id))
 AND associated.stored_path IS NOT NULL AND associated.stored_path=original.stored_path)"""
_OPERATION_LINKS="""associated.id=a.print_file_id OR associated.job_id=a.job_id OR associated.model_id=a.model_id
 OR associated.id=(SELECT print_file_id FROM models WHERE id=a.model_id)"""
_OPERATION_FROM="""education_upload_operations raw_o JOIN print_files associated
 ON associated.stored_path=raw_o.final_path AND raw_o.final_path IS NOT NULL"""
_EDUCATION_ASSOCIATION=("EXISTS (SELECT 1 FROM education_submissions raw_s WHERE "+_SUBMISSION_LINKS+") OR EXISTS (SELECT 1 FROM "+_OPERATION_FROM+" WHERE "+_OPERATION_LINKS+")")
_UNAMBIGUOUS_EDUCATION=("NOT EXISTS (SELECT 1 FROM education_submissions raw_s WHERE raw_s.id<>a.education_submission_id AND ("+_SUBMISSION_LINKS+")) AND NOT EXISTS (SELECT 1 FROM "+_OPERATION_FROM+" WHERE ("+_OPERATION_LINKS+") AND raw_o.operation_id<>(SELECT operation_id FROM education_submissions WHERE id=a.education_submission_id))")


def _file(db,identifier,user):
    if identifier is None or not _scope(user,'read:models'):return None
    # Read only identity and size-bounded thumbnail, never preview mesh bytes.
    row=db.execute(text('SELECT id,org_id,stored_path,CASE WHEN length(thumbnail_b64)<=:maximum THEN thumbnail_b64 ELSE NULL END AS thumbnail_b64 '
        'FROM print_files WHERE id=:id'),{'id':identifier,'maximum':4*((MAX_THUMBNAIL_BYTES+2)//3)}).first()
    if row is None or not check_org_access(user,row.org_id):return None
    # A plain alias of an Education original cannot become a fresh generic file.
    alias=db.execute(text("SELECT 1 FROM print_files target JOIN print_files original ON original.stored_path=target.stored_path "
        "JOIN education_submissions s ON s.print_file_id=original.id WHERE target.id=:id AND original.id<>target.id "
        "AND target.stored_path IS NOT NULL LIMIT 1"),{'id':identifier}).first()
    if alias:return None
    if is_education_owned_file(db,identifier,row.stored_path):
        provider=registry.get_provider('EducationPolicyProvider')
        authorize=getattr(provider,'assert_file_preview_allowed',None)
        if not callable(authorize):return None
        try:authorize(db,file_id=identifier,principal=user)
        except Exception:return None
    return row


def _model(db,identifier,user):
    if identifier is None or not _scope(user,'read:models'):return None
    row=db.execute(text('SELECT id,name,org_id,print_file_id FROM models WHERE id=:id'),{'id':identifier}).mappings().first()
    if row is None or not check_org_access(user,row['org_id']):return None
    protected=db.execute(text('SELECT 1 FROM education_submissions WHERE model_id=:id LIMIT 1'),{'id':identifier}).first()
    if row['print_file_id'] is not None:
        if _file(db,row['print_file_id'],user) is None:return None
    elif protected:return None
    return row


def project_recent(db,user,org_id=None):
    effective=_get_org_filter(user,org_id)
    tenant='(a.org_id=:org OR a.org_id IS NULL)' if should_filter_org(user,effective) else '1=1'
    provider=registry.get_provider('EducationPolicyProvider')
    audience=getattr(provider,'archive_audience_sql',None)
    # Require positive canonical association; canonical generic fallback cannot
    # authorize a marked alias with no exact current submission/job relationship.
    protected='(a.education_submission_id IS NOT NULL AND '+audience()+' AND '+_UNAMBIGUOUS_EDUCATION+')' if callable(audience) else '0=1'
    dialect=db.get_bind().dialect.name
    if dialect=='sqlite':
        db.connection().connection.driver_connection.create_function('odin_recent_terminal_time',1,_time_key,deterministic=True)
        order='odin_recent_terminal_time(a.actual_end)'
        timestamps='j.actual_start,j.actual_end'
    elif dialect=='postgresql':
        order='CASE WHEN isfinite(a.actual_end_sort) THEN a.actual_end_sort ELSE NULL END'
        timestamps='j.actual_start::text AS actual_start,j.actual_end::text AS actual_end,j.actual_end AS actual_end_sort'
    else:raise HTTPException(status_code=503,detail='Recent jobs unavailable')
    query=f"""SELECT a.id,a.job_id,a.item_name,a.status,a.print_file_id,a.model_id,a.printer_id,a.org_id,a.fail_reason,a.actual_start,a.actual_end,a.education_submission_id FROM (SELECT j.id,j.id AS job_id,j.item_name,j.status,j.print_file_id,j.model_id,j.printer_id,
 j.charged_to_org_id AS org_id,j.fail_reason,{timestamps},
 (SELECT MIN(s.id) FROM education_submissions s WHERE s.job_id=j.id) AS education_submission_id
 FROM jobs j WHERE j.status IN ('completed','failed')) a
 WHERE {tenant} AND (NOT ({_EDUCATION_ASSOCIATION}) OR {protected})
 ORDER BY {order} DESC NULLS LAST,a.id DESC LIMIT 8"""
    rows=db.execute(text(query),{'org':effective,'_arc_org':user.get('group_id'),'_arc_user':user['id'],
        '_arc_admin':1 if user.get('role')=='admin' and user.get('group_id') is not None else 0}).mappings().all()
    result=[]
    for row in rows:
        start=utc_time(row['actual_start']);end=utc_time(row['actual_end'])
        duration=(end-start).total_seconds() if start is not None and end is not None and end>=start else None
        model=_model(db,row['model_id'],user)
        identifier=row['print_file_id'] if row['print_file_id'] is not None else model['print_file_id'] if model else None
        file=_file(db,identifier,user)
        printer=None
        if row['printer_id'] is not None and _scope(user,'read:printers'):
            candidate=db.execute(text('SELECT name,org_id,shared FROM printers WHERE id=:id'),{'id':row['printer_id']}).mappings().first()
            if candidate and (check_org_access(user,candidate['org_id']) or candidate['shared'] in (True,1)):printer=candidate['name']
        result.append(dict(id=row['id'],item_name=row['item_name'],status=row['status'],actual_end=end.isoformat().replace('+00:00','Z') if end else None,
            duration_seconds=duration,printer_name=printer,model_name=model['name'] if model else None,fail_reason=row['fail_reason'],
            thumbnail_data=thumbnail_data(file.thumbnail_b64) if file else None))
    return result


router=APIRouter(prefix='/jobs',tags=['Jobs'])


@router.get('/recent-terminal',response_model=list[RecentTerminalJob])
def recent_terminal(org_id:int|None=Query(default=None,gt=0),user:dict=Depends(require_role('viewer',scope='read:jobs')),db:Session=Depends(get_db)):
    return project_recent(db,user,org_id)
