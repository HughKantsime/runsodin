"""Education ranks permute only Education slots in the existing generic order."""
from collections import defaultdict
from sqlalchemy import text


def education_order(rows, education_ids, value=lambda row, key: getattr(row, key)):
    result = list(rows)
    slots = defaultdict(list)
    for index, row in enumerate(rows):
        if value(row, 'id') in education_ids:
            slots[value(row, 'printer_id')].append(index)
    for indices in slots.values():
        ordered = sorted((rows[i] for i in indices), key=lambda row: (
            value(row, 'queue_position') is None,
            value(row, 'queue_position') if value(row, 'queue_position') is not None else 0))
        for index, row in zip(indices, ordered):
            result[index] = row
    return result


def waiting_rows(conn, printer_id):
    return [dict(row._mapping) for row in conn.execute(text(
        "SELECT j.id,j.printer_id,j.queue_position,j.priority,j.created_at,j.status,j.hold,"
        "s.org_id,s.cost_center_id,s.lifecycle_revision,s.id AS submission_id,"
        "EXISTS(SELECT 1 FROM printer_dispatch_claims d WHERE d.job_id=j.id) AS committed,"
        "EXISTS(SELECT 1 FROM education_monitor_claims m WHERE m.job_id=j.id) AS monitor_claim "
        "FROM jobs j JOIN education_submissions s ON s.job_id=j.id "
        "WHERE j.printer_id=:pid AND j.status IN ('pending','scheduled') "
        "ORDER BY j.priority,(j.queue_position IS NULL),j.queue_position,j.created_at,j.id"
    ), {'pid': printer_id})]


def ordered_waiting(conn, printer_id):
    rows = waiting_rows(conn, printer_id)
    rows = [row for row in rows if not row['committed'] and not row['monitor_claim']]
    return education_order(rows, {row['id'] for row in rows}, lambda row, key: row[key])


def head_refusal(conn, job):
    if job.get('education_submission_id') is None:
        return None
    rows = ordered_waiting(conn, job['printer_id'])
    if not rows or rows[0]['id'] != job['id']:
        return 'Education queue head must start first on this printer; reload the Job Queue'
    return None
