"""Explicit bounded legacy repair; never infer content ownership from devices."""
import json
from sqlalchemy import bindparam, text, Text
from .ownership import capture_content_owner

# All batches acquire these row locks in this order and ascending key order.
# Key-column no-op UPDATE uses a SQLite writer lock and PostgreSQL FOR UPDATE
# strength, stabilizing existing source identities and FK child insertions.
_LOCK_KEYS = {'print_archives':'id','print_jobs':'id','jobs':'id','print_files':'id',
              'models':'id','education_submissions':'id','users':'id',
              'education_upload_operations':'operation_id'}


def _lock_rows(db, table, identifiers):
    key = _LOCK_KEYS[table]  # Only these internal symbols reach SQL.
    for identifier in sorted(set(x for x in identifiers if x is not None)):
        # Repair must not block while holding a different callback's source lock.
        # Education and generic completion intentionally have different orders.
        if db.get_bind().dialect.name == 'postgresql':
            db.execute(text(f'SELECT {key} FROM {table} WHERE {key}=:key FOR UPDATE NOWAIT'),  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- table/key come from the fixed _LOCK_KEYS map; values are bound
                       {'key': identifier})
        db.execute(text(f'UPDATE {table} SET {key}={key} WHERE {key}=:key'), {'key':identifier})  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- table/key come from the fixed _LOCK_KEYS map; values are bound


def _rows(db, table, identifiers):
    key = _LOCK_KEYS[table]
    values = sorted(set(x for x in identifiers if x is not None))
    if not values:
        return []
    return db.execute(text(f'SELECT * FROM {table} WHERE {key} IN :keys')  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- table/key come from the fixed _LOCK_KEYS map; values are bound
                      .bindparams(bindparam('keys',expanding=True)), {'keys':values}).mappings().all()


def _lock_sources(db, archives):
    if not archives:
        return
    _lock_rows(db,'print_jobs',[a['print_job_id'] for a in archives])
    _lock_rows(db,'jobs',[a['job_id'] for a in archives])
    _lock_rows(db,'print_files',[a['print_file_id'] for a in archives])
    jobs = _rows(db,'jobs',[a['job_id'] for a in archives])
    files = _rows(db,'print_files',[a['print_file_id'] for a in archives])
    _lock_rows(db,'models',[r['model_id'] for r in jobs+files])
    job_ids = [a['job_id'] for a in archives if a['job_id'] is not None]
    file_ids = [a['print_file_id'] for a in archives if a['print_file_id'] is not None]
    markers = [a['education_submission_id'] for a in archives if a['education_submission_id'] is not None]
    query=text('SELECT id FROM education_submissions WHERE job_id IN :jobs OR print_file_id IN :files OR id IN :markers').bindparams(
        bindparam('jobs',expanding=True),bindparam('files',expanding=True),bindparam('markers',expanding=True))
    submission_ids=db.execute(query,{'jobs':job_ids,'files':file_ids,'markers':markers}).scalars().all()
    _lock_rows(db,'education_submissions',submission_ids)
    submissions=_rows(db,'education_submissions',submission_ids)
    _lock_rows(db,'users',[a['user_id'] for a in archives]+[j['submitted_by'] for j in jobs]
               +[f['created_by'] for f in files]+[s['submitted_by'] for s in submissions])
    paths=[f['stored_path'] for f in files if f['stored_path'] is not None]
    operation_ids=[s['operation_id'] for s in submissions]
    linked=db.execute(text('SELECT operation_id FROM education_upload_operations WHERE final_path IN :paths')
                      .bindparams(bindparam('paths',type_=Text(),expanding=True)),{'paths':paths}).scalars().all()
    _lock_rows(db,'education_upload_operations',operation_ids+linked)


def _resolve_locked(db, archive):
    if archive['job_id'] is None or archive['print_file_id'] is None:
        return None
    # Existing conflicts require explicit evidence review, not automatic relabeling.
    if archive['ownership_state']=='unresolved' and archive['ownership_source'] not in (None,'missing_job_file'):
        return None
    proposal=capture_content_owner(db.connection(),archive['job_id'])
    if proposal['state']!='known' or proposal['print_file_id']!=archive['print_file_id']:
        return None
    if archive['org_id'] is not None and archive['org_id']!=proposal['org_id']:
        return None
    if archive['education_submission_id'] is not None and archive['education_submission_id']!=proposal['education_submission_id']:
        return None
    file=db.execute(text('SELECT stored_path FROM print_files WHERE id=:id'),{'id':archive['print_file_id']}).first()
    if archive['file_path'] is not None and (not file or archive['file_path']!=file.stored_path):
        return None
    job=db.execute(text('SELECT submitted_by FROM jobs WHERE id=:id'),{'id':archive['job_id']}).first()
    if archive['user_id'] is not None and (not job or job.submitted_by!=archive['user_id']):
        return None
    source='legacy_job_file'
    if archive['print_job_id'] is not None:
        attempt=db.execute(text('SELECT scheduled_job_id,spool_snapshot FROM print_jobs WHERE id=:id'),{'id':archive['print_job_id']}).first()
        if not attempt or attempt.scheduled_job_id!=archive['job_id']:
            return None
        if attempt.spool_snapshot is not None:
            try:
                snapshot=json.loads(attempt.spool_snapshot) if isinstance(attempt.spool_snapshot,str) else attempt.spool_snapshot
                owner=snapshot.get('content_owner') if isinstance(snapshot,dict) else None
            except (ValueError,TypeError):
                return None
            if owner is not None:
                keys=('state','org_id','scheduled_job_id','print_file_id','education_submission_id')
                if not isinstance(owner,dict) or any(owner.get(k)!=proposal.get(k) for k in keys):
                    return None
                source='legacy_first_start'
            else:
                plan=snapshot.get('material_plan') if isinstance(snapshot,dict) else None
                if isinstance(plan,dict):
                    for key in ('scheduled_job_id','print_file_id','org_id'):
                        if key in plan and plan[key]!=proposal.get(key):
                            return None
    return dict(proposal,source=source)


def resolve_legacy_archive_ownership(db, *, limit=100, after_archive_id=0):
    """Caller owns one transaction; failures roll back the whole explicit batch.

    Known anchors, accounting and inventory are never modified. No device,
    provider or notification calls occur. Pending rows survive pagination.
    """
    if type(limit) is not int or not 1<=limit<=100 or type(after_archive_id) is not int or after_archive_id<0:
        raise ValueError('Invalid reconciliation batch')
    ids=db.execute(text("SELECT id FROM print_archives WHERE id>:cursor AND (ownership_state IS NULL OR ownership_state='unresolved') ORDER BY id LIMIT :limit"),
                   {'cursor':after_archive_id,'limit':limit}).scalars().all()
    _lock_rows(db,'print_archives',ids)
    archives=[a for a in _rows(db,'print_archives',ids) if a['ownership_state'] in (None,'unresolved')]
    archives.sort(key=lambda a:a['id'])
    _lock_sources(db,archives)
    reconciled=0
    for archive in archives:
        proposal=_resolve_locked(db,archive)
        if proposal is None:
            continue
        changed=db.execute(text("UPDATE print_archives SET org_id=:org,ownership_state='known',ownership_source=:source,education_submission_id=:edu WHERE id=:id AND (ownership_state IS NULL OR ownership_state='unresolved')"),
            {'org':proposal['org_id'],'source':proposal['source'],'edu':proposal['education_submission_id'],'id':archive['id']})
        reconciled+=changed.rowcount
    pending=db.execute(text("SELECT COUNT(*) FROM print_archives WHERE ownership_state IS NULL OR ownership_state='unresolved'")).scalar_one()
    return {'attempted':len(archives),'reconciled':reconciled,'pending_count':pending,
            'next_after_id':max(ids,default=after_archive_id)}
