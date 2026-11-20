"""Attach a history job to an observed print end in the caller's transaction."""
from sqlalchemy import text

from core.db_compat import as_utc_datetime
from core.dispatch_observation_executor import TransactionView
from core.display_names import observed_item_name
from core.interfaces.education_policy import is_education_reserved_filename
from core.job_identity import insert_job
from core.print_attempts import bind_print_attempt, sqlalchemy_runner


def link_ended_print(executor, print_job_id, *, duration_hours=None):
    """Caller holds the print's write lock. No events, accounting or commit here."""
    db = TransactionView(executor)
    row = db.execute(text(
        'SELECT pj.printer_id,pj.scheduled_job_id,pj.status,pj.started_at,pj.ended_at,'
        'pj.filename,pj.job_name,pj.print_duration_seconds,p.org_id '
        'FROM print_jobs pj JOIN printers p ON p.id=pj.printer_id WHERE pj.id=:id'
    ), {'id': print_job_id}).first()
    if row is None or row[1] is not None:
        return row[1] if row else None
    printer_id, _, status, started, ended, filename, name, seconds, org_id = row
    if (status not in ('completed', 'failed', 'cancelled') or ended is None
            or is_education_reserved_filename(filename) or is_education_reserved_filename(name)):
        return None
    if duration_hours is None:
        if seconds is None and started is not None:
            try:
                seconds = (as_utc_datetime(ended) - as_utc_datetime(started)).total_seconds()
            except (TypeError, ValueError):
                pass
        duration_hours = round(seconds / 3600, 4) if seconds is not None else None
    job_id = insert_job(executor, '''
        INSERT INTO jobs (item_name, printer_id, status, actual_start, actual_end,
                         duration_hours, quantity, hold, is_locked, quantity_on_bed, charged_to_org_id)
        VALUES (:name, :printer, :status, :start, :end, :hours, 1, FALSE, FALSE, 1, :org)
        RETURNING id
    ''', {'name': observed_item_name(executor, print_job_id), 'printer': printer_id,
          'status': status, 'start': started, 'end': ended,
          'hours': duration_hours, 'org': org_id}).scalar_one()
    db.execute(text('UPDATE print_jobs SET scheduled_job_id=:job WHERE id=:id'),
               {'job': job_id, 'id': print_job_id})
    bind_print_attempt(sqlalchemy_runner(db), print_job_id, job_id)
    return job_id
