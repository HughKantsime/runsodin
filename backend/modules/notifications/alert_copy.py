"""Human alert copy shared by API, daemon and outbox delivery."""
import re
from pathlib import PurePosixPath
from sqlalchemy import text


def human_name(*values):
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        value = PurePosixPath(value.strip().replace('\\', '/')).name
        if 'odin-dispatch-' in value.lower() or value.lower() in {'unknown', 'none'}:
            continue
        for suffix in ('.gcode.3mf', '.gcode', '.bgcode', '.gco', '.3mf', '.stl'):
            if value.lower().endswith(suffix):
                value = value[:-len(suffix)]
                break
        if value:
            return value
    return 'print'


def job_names(db, job_id):
    if job_id is None:
        return ()
    return db.execute(text('SELECT j.item_name,f.project_name,f.original_filename,f.filename '
                           'FROM jobs j LEFT JOIN print_files f ON f.id=j.print_file_id WHERE j.id=:id'),
                      {'id': job_id}).first() or ()


def reason_for(code):
    from modules.printers.hms_codes import lookup_hms_code
    if code is None or code == 0:
        return ''
    if isinstance(code, int) or str(code).isdigit():
        value = f'{int(code):08X}'
        code = value[:4] + '_' + value[4:]
    reason = lookup_hms_code(str(code))
    if reason.startswith(('Unknown', 'HMS error:')) or '0x' in reason:
        return 'The printer needs attention.'
    return reason


def humanize(db, alert_type, title, message, printer_id, job_id, metadata=None):
    kind = getattr(alert_type, 'value', alert_type)
    if kind not in {'print_complete', 'print_failed', 'print_cancelled', 'print_paused', 'printer_error'}:
        return title, message, metadata
    # Context-free integration notifications already supply their own copy.
    # Printer/job alerts (and opaque dispatch names) use the human lifecycle copy.
    if printer_id is None and job_id is None and 'odin-dispatch-' not in title.lower() and kind not in {'print_paused', 'print_cancelled'}:
        return title, message, metadata
    meta = dict(metadata or {})
    printer = db.execute(text('SELECT name,nickname FROM printers WHERE id=:id'), {'id': printer_id}).first()
    nickname = (printer[1] or printer[0]) if printer else 'Printer'
    candidates = list(job_names(db, job_id))
    candidates.extend([meta.get('job_name'), meta.get('filename')])
    # Only an active observation is relevant to resource-only errors/pauses.
    if job_id is None and printer_id is not None:
        active = db.execute(text("SELECT j.item_name,f.project_name,f.original_filename,p.job_name,p.filename "
                                 "FROM print_jobs p LEFT JOIN jobs j ON j.id=p.scheduled_job_id "
                                 "LEFT JOIN print_files f ON f.id=j.print_file_id "
                                 "WHERE p.printer_id=:id AND p.status IN ('running','printing','paused') "
                                 "ORDER BY p.id DESC LIMIT 1"), {'id': printer_id}).first()
        if active:
            candidates.extend(active)
    # Legacy terminal callbacks carry the observed name in their title.
    if ': ' in title and kind.startswith('print_'):
        candidates.append(title.split(': ', 1)[1].split(' (', 1)[0])
    name = human_name(*candidates)
    if 'job_name' in meta:
        meta['job_name'] = name
    if isinstance(meta.get('summary'), dict):
        summary = dict(meta['summary'])
        previous_name = summary.get('job_name')
        summary['job_name'] = name
        if previous_name and 'odin-dispatch-' in str(previous_name).lower():
            summary['text'] = str(summary.get('text', '')).replace(previous_name, name)
        meta['summary'] = summary
    happened = {'print_complete': 'finished', 'print_failed': 'failed', 'print_cancelled': 'was cancelled',
                'print_paused': 'paused before printing' if meta.get('before_printing') else 'paused',
                'printer_error': 'needs attention'}[kind]
    title = f'{nickname}: {name} {happened}'[:200]
    action = {'print_complete': 'Check the print and clear the bed when it is safe.',
              'print_failed': 'Check the printer and filament before retrying.',
              'print_cancelled': 'Clear the bed when it is safe, then requeue the print if needed.',
              'print_paused': 'Check the printer and filament, then retry or resume.',
              'printer_error': 'Check the printer before continuing.'}[kind]
    code = meta.get('hms_code') or meta.get('code') or meta.get('error_code')
    match = re.search(r'\[([^\]:]+):([^\]]+)\]', message or '')
    if match:
        code = code or match[2]
        meta.setdefault('source', match[1])
        meta.setdefault('code', code)
    reason = reason_for(code) if code else meta.get('pause_reason', '')
    # Non-code errors from other protocols retain their useful explanation.
    if not reason and kind in {'printer_error', 'print_failed'}:
        reason = re.sub(r'\[[^\]]+\]', '', message or '').strip()
        reason = re.sub(r'\bodin-dispatch-[\w-]+(?:\.[\w.]+)?', name, reason, flags=re.IGNORECASE)
    message = ' '.join(part for part in (reason, action) if part)
    if code:
        source = meta.get('source', 'hms' if '_' in str(code) else 'print_error')
        message += f'\nDetails: [{source}:{code}]'
    return title, message, meta
