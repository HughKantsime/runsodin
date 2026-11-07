"""Append-only usage can arrive from archive accounting after terminal capture."""
from core.print_attempts import bound_print_attempt, sqlalchemy_runner, moment, attempt_starts


def attempt_usage(db, job, seq, reset, *, bound_only=False):
    from core.traceability import rows
    run = sqlalchemy_runner(db)
    usage = rows(db, '''SELECT u.id,u.spool_id,u.weight_used_g,u.used_at,u.print_job_id,
        s.org_id AS spool_org_id,f.material,s.color_name,COALESCE(s.color_hex,f.color_hex) AS colour,
        s.lot_number,s.vendor,f.brand FROM spool_usage u
        LEFT JOIN spools s ON s.id=u.spool_id LEFT JOIN filament_library f ON f.id=s.filament_id
        WHERE u.job_id=:id ORDER BY u.id''', id=job['id'])
    # An explicit physical binding wins over wall-clock windows: late archive
    # accounting can arrive after reset. Legacy unbound usage needs both bounds.
    end = attempt_starts(run, job['id']).get(seq + 1)
    def belongs(item):
        if item['print_job_id'] is not None:
            return bound_print_attempt(run, item['print_job_id'], job['id']) == seq
        if bound_only or (seq > 0 and reset is None):
            return False
        at = moment(item['used_at'])
        return ((reset is None or (at is not None and at >= moment(reset))) and
                (end is None or (at is not None and at < end)))
    usage = [u for u in usage if belongs(u)]
    for item in usage:
        if item.pop('spool_org_id') not in (None, job['charged_to_org_id']):
            for field in ('spool_id','material','color_name','colour','lot_number','vendor','brand'):
                item[field] = None
    return usage


def rendered_usage(db, job, snapshot):
    """Retain captured descriptions; supplement only explicit attempt evidence.

    The snapshot digest covers capture-time facts. Late ledger rows are separate
    evidence and never rewrite that digest or the signed capture-time snapshot.
    """
    captured = {u['id']:u for u in snapshot.get('filament', [])}
    for usage in attempt_usage(db, job, snapshot['attempt'], None, bound_only=True):
        if usage['id'] in captured:
            continue
        # Prefer descriptions frozen with the physical start evidence. Otherwise
        # report the actual ledger fact with explicit description provenance.
        for evidence in snapshot.get('attempt_material_evidence', []):
            if evidence['print_job_id'] != usage['print_job_id']:
                continue
            for spool in evidence.get('spools', []):
                if spool['spool_id'] == usage['spool_id']:
                    usage.update({k:spool.get(k) for k in ('material','color_name','lot_number','vendor','brand')})
                    usage['colour'] = spool.get('color_hex')
                    break
        usage['recorded_after_capture'] = True
        captured[usage['id']] = usage
    return [captured[key] for key in sorted(captured)]
