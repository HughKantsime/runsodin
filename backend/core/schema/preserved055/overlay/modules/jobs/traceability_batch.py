"""Bounded exports: short database transactions and disk-backed ZIP streaming."""
import json
import tempfile
import zipfile
from datetime import timezone
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from sqlalchemy import text
from core.dependencies import log_audit
from modules.jobs.traceability_certificate import certificate, signed, signing_identity, certificate_branding

MAX_JOBS = 500
MAX_BYTES = 100 * 1024 * 1024


def export_batch(db, user, start, end, load):
    if start.tzinfo is None or end.tzinfo is None:
        raise HTTPException(422, 'Date range requires explicit UTC offsets')
    start, end = (v.astimezone(timezone.utc).replace(tzinfo=None) for v in (start, end))
    if end <= start or (end-start).total_seconds() > 366*86400:
        raise HTTPException(422, 'Choose a positive date range of at most 366 days (end exclusive)')
    identity = signing_identity()
    platform = user['role'] == 'admin' and user.get('group_id') is None
    from core.controlled_parts import job_visibility
    visibility, controlled_values = job_visibility(user, 'jobs')
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- job_visibility validates the jobs alias and emits fixed policy SQL; actor, dates, owner and limit are bound
    ids = db.execute(text('''SELECT id FROM jobs WHERE status='completed' AND actual_end>=:start AND actual_end<:end
        AND (:platform=1 OR charged_to_org_id=:owner)
        AND NOT EXISTS(SELECT 1 FROM education_submissions s WHERE s.job_id=jobs.id)
        AND (''' + visibility + ''') ORDER BY id LIMIT :limit'''), {**controlled_values, 'start':start,'end':end,'platform':int(platform),'owner':user.get('group_id'), 'limit':MAX_JOBS+1}).scalars().all()
    db.rollback()
    if len(ids) > MAX_JOBS:
        raise HTTPException(413, f'More than {MAX_JOBS} jobs; choose a smaller date range')
    output = tempfile.SpooledTemporaryFile(max_size=1024*1024, mode='w+b')
    manifest = {'start':start.isoformat()+'Z', 'end':end.isoformat()+'Z', 'job_ids':[], 'skipped':[]}
    total = 0
    try:
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            for job_id in ids:
                try:
                    job, retained, record = load(db, user, job_id)
                    branding = certificate_branding(db, job['charged_to_org_id'])
                    # Legacy capture may write once; retained records use SELECTs.
                    db.commit()
                    pdf = certificate(None, record, job['charged_to_org_id'], branding=branding, identity=identity)
                    metadata = signed(pdf, identity)
                except HTTPException as exc:
                    db.rollback()
                    if exc.status_code not in (403, 404, 409, 422):
                        raise
                    manifest['skipped'].append({'job_id':job_id, 'status':exc.status_code, 'reason':exc.detail})
                    continue
                files = {f'job-{job_id}.pdf':pdf,
                         f'job-{job_id}.signature.json':json.dumps(metadata).encode(),
                         f'job-{job_id}.json':json.dumps(jsonable_encoder(record), ensure_ascii=False).encode()}
                total += sum(len(value) for value in files.values())
                if total > MAX_BYTES:
                    raise HTTPException(413, 'Traceability export byte cap exceeded; choose a smaller date range')
                for name, data in files.items():
                    archive.writestr(name, data)
                if output.tell() > MAX_BYTES:
                    raise HTTPException(413, 'Traceability export byte cap exceeded; choose a smaller date range')
                manifest['job_ids'].append(job_id)
            encoded = json.dumps(manifest).encode()
            if total + len(encoded) > MAX_BYTES:
                raise HTTPException(413, 'Traceability export byte cap exceeded; choose a smaller date range')
            archive.writestr('manifest.json', encoded)
        if output.tell() > MAX_BYTES:
            raise HTTPException(413, 'Traceability export byte cap exceeded; choose a smaller date range')
        log_audit(db, 'traceability.batch_requested', details=manifest)
        db.commit()
        output.seek(0)
    except BaseException:
        db.rollback()
        output.close()
        raise

    def chunks():
        try:
            while data := output.read(64*1024):
                yield data
        finally:
            output.close()
    return StreamingResponse(chunks(), media_type='application/zip',
        headers={'Content-Disposition':'attachment; filename="traceability.zip"', 'Cache-Control':'no-store'},
        background=BackgroundTask(output.close))
