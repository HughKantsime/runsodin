"""Bind printer-side repeats to controlled receipt owners for confidentiality.

A historical receipt is not authority to revive a scheduled job, dispatch it,
charge it or promote the attempt's permanently unresolved economic identity.
"""
from sqlalchemy import text
from core.dispatch_receipts import generic_observed_token, observed_filename_matches
from core.controlled_parts import job_visibility


def bind_reprint(view, print_job_id, printer_id, protocol, names, key):
    row = view.execute(text('SELECT scheduled_job_id FROM print_jobs WHERE id=:id AND printer_id=:printer'), {'id': print_job_id, 'printer': printer_id}).first()
    if row is None or row[0] is not None:
        return row[0] if row else None
    hidden, values = job_visibility(None, 'j')
    for name in names:
        token = generic_observed_token(protocol, name)
        if token is None:
            continue
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- selected file subquery is source-owned with a fixed job alias; receipt identity is bound
        receipt = view.execute(text('SELECT r.job_id,r.observed_filename_key FROM dispatch_attempt_receipts r '
            'JOIN jobs j ON j.id=r.job_id WHERE r.claim_token=:token AND r.printer_id=:printer '
            'AND NOT EXISTS (SELECT 1 FROM education_submissions e WHERE e.job_id=j.id) AND NOT (' + hidden + ')'),
            {**values, 'token': token, 'printer': printer_id}).first()
        if receipt and observed_filename_matches(key, token, protocol, name, receipt[1]):
            changed = view.execute(text('UPDATE print_jobs SET scheduled_job_id=:job WHERE id=:id AND scheduled_job_id IS NULL'),
                                  {'job': receipt[0], 'id': print_job_id})
            if changed.rowcount:
                return receipt[0]
    return None
