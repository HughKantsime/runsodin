"""Media content authorization from immutable capture facts and current audience."""
from fastapi import HTTPException
from sqlalchemy import text
from core.rbac import get_org_scope,should_filter_org,check_org_access
from core.registry import registry
from core.interfaces.education_policy import generic_archive_audience_sql

_PROJECTION = """(SELECT operation_id,timelapse_id,printer_id,scheduled_job_id AS job_id,
    filename,owner_state,owner_org_id AS org_id,print_file_id,education_submission_id
    FROM timelapse_ownership_anchors) a"""
_TUPLE = """a.operation_id=timelapses.ownership_operation_id
    AND a.timelapse_id=timelapses.id AND a.printer_id=timelapses.printer_id
    AND a.filename=timelapses.filename
    AND (a.job_id=timelapses.print_job_id OR (a.job_id IS NULL AND timelapses.print_job_id IS NULL))"""


def immutable_timelapse_policy(principal,*,mutation=False):
    if not principal:return '1=0',{}
    org=get_org_scope(principal)
    platform=not should_filter_org(principal,org)
    if platform and not mutation:return '1=1',{}
    if platform:
        policy="a.owner_state IN ('known','shared')"
        parameters={}
    else:
        provider=registry.get_provider('EducationPolicyProvider')
        audience=getattr(provider,'archive_audience_sql',None)
        education=audience() if callable(audience) else generic_archive_audience_sql()
        policy="((a.owner_state='known' AND a.org_id=:_arc_org) OR a.owner_state='shared') AND ("+education+")"
        parameters={'_arc_org':org,'_arc_user':principal.get('id'),'_arc_admin':1 if principal.get('role')=='admin' else 0}
    return policy,parameters


def timelapse_visibility(principal,*,mutation=False):
    policy,parameters=immutable_timelapse_policy(principal,mutation=mutation)
    if policy=='1=1' or policy=='1=0':return policy,parameters
    return 'EXISTS (SELECT 1 FROM '+_PROJECTION+' WHERE '+_TUPLE+' AND '+policy+')',parameters


def assert_timelapse_access(db,media,principal,*,mutation=False):
    condition,parameters=timelapse_visibility(principal,mutation=mutation)
    # Bind the loaded object too: authorizing a newly replaced row by ID must
    # never permit reading an older ORM object's filename or ownership.
    identity="""timelapses.id=:_media_id AND timelapses.printer_id=:_media_printer
        AND timelapses.filename=:_media_filename
        AND (timelapses.print_job_id=:_media_job OR (timelapses.print_job_id IS NULL AND :_media_job IS NULL))
        AND (timelapses.ownership_operation_id=:_media_nonce OR (timelapses.ownership_operation_id IS NULL AND :_media_nonce IS NULL))"""
    found=db.execute(text('SELECT timelapses.id FROM timelapses WHERE '+identity+' AND '+condition),{
        **parameters,'_media_id':media.id,'_media_printer':media.printer_id,
        '_media_filename':media.filename,'_media_job':media.print_job_id,'_media_nonce':media.ownership_operation_id,
    }).first()
    from modules.printers.models import Printer
    printer=db.query(Printer).filter(Printer.id==media.printer_id).first() if found else None
    if found is None or printer is None:raise HTTPException(status_code=404,detail='Timelapse not found')
    return printer


def visible_printer_name(db,media,principal):
    # Read raw values so SQLite Boolean coercion cannot turn stored 2/'false'
    # into a grant for private current-printer metadata.
    printer=db.execute(text('SELECT name,org_id,shared FROM printers WHERE id=:id'),{'id':media.printer_id}).first()
    if printer is None:return None
    value=printer[2]
    shared=value is True or (type(value) is int and value==1)
    return printer[0] if check_org_access(principal,printer[1]) or shared else None


def inherit_timelapse_owner(db,source,derived,principal):
    """Stage a fresh tuple-bound copy of an existing immutable owner, no commit."""
    assert_timelapse_access(db,source,principal,mutation=True)
    import uuid
    from core.timelapse_ownership_schema import anchors
    captured=db.execute(anchors.select().where(anchors.c.operation_id==source.ownership_operation_id)).mappings().first()
    if captured is None or captured['owner_state'] not in ('known','shared') or (
        captured['timelapse_id'],captured['printer_id'],captured['scheduled_job_id'],captured['filename']
    )!=(source.id,source.printer_id,source.print_job_id,source.filename):
        raise HTTPException(status_code=409,detail='Media ownership changed')
    nonce=uuid.uuid4().hex
    values={key:captured[key] for key in anchors.c.keys() if key!='created_at'}
    values.update(operation_id=nonce,timelapse_id=derived.id,printer_id=derived.printer_id,
                  scheduled_job_id=derived.print_job_id,filename=derived.filename)
    derived.ownership_operation_id=nonce
    db.execute(anchors.insert(),values)
