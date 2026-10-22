"""Bounded insertion-fenced content aliases for private financial reads."""
from fastapi import HTTPException
from sqlalchemy import text,bindparam,Integer,String
from core.registry import registry
from modules.archives.ownership import capture_content_owner

_LIMIT=16
_EXPANSION_TYPES={'files':Integer,'models':Integer,'paths':String,'operations':String}
_TABLES=('education_cost_center_grants','education_cost_centers','education_submissions',
         'education_upload_operations','models','print_files','users')

_MODEL_ALIASES='SELECT id,print_file_id,org_id FROM models WHERE id IN :models OR print_file_id IN :files'
_FILE_ALIASES='SELECT id,model_id,stored_path,org_id FROM print_files WHERE id IN :files OR model_id IN :models OR job_id=:job OR stored_path IN :paths'
_SUBMISSION_ALIASES='SELECT id,job_id,print_file_id,model_id,operation_id FROM education_submissions WHERE job_id=:job OR print_file_id IN :files OR model_id IN :models OR operation_id IN :operations'
_OPERATION_ALIASES='SELECT operation_id,final_path FROM education_upload_operations WHERE operation_id IN :operations OR final_path IN :paths'
# Closed set of statements _bounded may run.
_BOUNDED_STATEMENTS=frozenset({_MODEL_ALIASES,_FILE_ALIASES,_SUBMISSION_ALIASES,_OPERATION_ALIASES})


def fence_content_membership(db):
    if db.get_bind().dialect.name=='postgresql':
        db.execute(text('LOCK TABLE '+','.join('public.'+name for name in _TABLES)+' IN SHARE MODE NOWAIT'))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- tables are the _TABLES module constant
    else:
        db.execute(text('UPDATE print_files SET id=id WHERE 0'))


def _refuse():
    raise HTTPException(404,'Attempt valuation not found')


def _bounded(db,sql,params,expanding=()):
    if any(key not in _EXPANSION_TYPES for key in expanding):
        raise ValueError('Unknown content alias expansion domain')
    if sql not in _BOUNDED_STATEMENTS:
        raise ValueError('Unsupported content alias statement')
    query=text(sql+' LIMIT 17')  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- statement checked against the _BOUNDED_STATEMENTS allowlist; values are bound
    if expanding:query=query.bindparams(*(bindparam(key,expanding=True,type_=_EXPANSION_TYPES[key]) for key in expanding))
    rows=db.execute(query,params).mappings().all()
    if len(rows)>_LIMIT:_refuse()
    return rows


def assert_content_visible(db,*,graph,principal):
    """Caller has fenced membership and locked/reproved financial parents."""
    job=db.execute(text('SELECT print_file_id,model_id FROM jobs WHERE id=:id'),{'id':graph['job_id']}).first()
    if job is None:_refuse()
    files=set([job[0]]) if job[0] is not None else set()
    models=set([job[1]]) if job[1] is not None else set()
    paths=set();submissions={};operations={};file_rows={}
    for _ in range(_LIMIT+1):
        previous=(frozenset(files),frozenset(models),frozenset(paths),frozenset(submissions),frozenset(operations))
        model_rows=_bounded(db,_MODEL_ALIASES,{'models':sorted(models),'files':sorted(files)},('models','files'))
        for row in model_rows:
            models.add(row['id'])
            if row['print_file_id'] is not None:files.add(row['print_file_id'])
        matches=_bounded(db,_FILE_ALIASES,{'files':sorted(files),'models':sorted(models),'job':graph['job_id'],'paths':sorted(paths)},('files','models','paths'))
        for row in matches:
            files.add(row['id']);file_rows[row['id']]=row
            if row['model_id'] is not None:models.add(row['model_id'])
            if row['stored_path'] is not None:paths.add(row['stored_path'])
        refs=_bounded(db,_SUBMISSION_ALIASES,{'job':graph['job_id'],'files':sorted(files),'models':sorted(models),'operations':sorted(operations)},('files','models','operations'))
        for row in refs:
            submissions[row['id']]=row
            if row['print_file_id'] is not None:files.add(row['print_file_id'])
            if row['model_id'] is not None:models.add(row['model_id'])
            if row['operation_id'] is not None:operations.setdefault(row['operation_id'],None)
        ops=_bounded(db,_OPERATION_ALIASES,{'operations':sorted(operations),'paths':sorted(paths)},('operations','paths'))
        for row in ops:
            operations[row['operation_id']]=row
            if row['final_path'] is not None:paths.add(row['final_path'])
        if any(len(values)>_LIMIT for values in (files,models,paths,submissions,operations)):_refuse()
        current=(frozenset(files),frozenset(models),frozenset(paths),frozenset(submissions),frozenset(operations))
        if current==previous:break
    else:_refuse()
    if files-set(file_rows) or models-{row['id'] for row in model_rows}:_refuse()
    expected_org=graph['owner_key'] if graph['owner_scope']=='org' else None
    if any(row['org_id']!=expected_org for row in file_rows.values()) or any(row['org_id']!=expected_org for row in model_rows):_refuse()
    if submissions or operations:
        canonical=capture_content_owner(db.connection(),graph['job_id'])
        if canonical['state']!='known' or canonical['org_id']!=expected_org or canonical['education_submission_id'] is None:_refuse()
        if canonical['education_submission_id'] not in submissions:_refuse()
        provider=registry.get_provider('EducationPolicyProvider')
        if provider is None:_refuse()
        represented={row['operation_id'] for row in submissions.values()}
        if set(operations)-represented:_refuse()
        marked_files={row['print_file_id'] for row in submissions.values()}
        if None in marked_files:_refuse()
        for identifier in sorted(marked_files):
            provider.assert_file_preview_allowed(db,file_id=identifier,principal=principal)
    return tuple(sorted(files))
