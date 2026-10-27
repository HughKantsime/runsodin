"""Native notification categories, safe copy, and Education delivery boundary."""
import re
from sqlalchemy import text

CATEGORIES = {
    'print_complete': 'PRINT_COMPLETE', 'print_failed': 'PRINT_FAILED',
    'printer_error': 'HMS_ERROR', 'spaghetti_detected': 'SPAGHETTI_DETECTED',
    'detachment_detected': 'SPAGHETTI_DETECTED', 'first_layer_issue': 'FIRST_LAYER_ISSUE',
    'spool_low': 'SPOOL_LOW', 'job_submitted': 'JOB_APPROVAL_REQUIRED',
    'printer_offline': 'PRINTER_OFFLINE',
}


def education_scope(db, *, org_id=None, job_id=None, printer_id=None):
    """Fail closed for Education mode and Education-owned jobs/active printers."""
    mode = db.execute(text("SELECT value FROM system_config WHERE key='education_mode'")).scalar()
    if str(mode).strip('"').lower() == 'true':
        return True
    if job_id is not None and db.execute(text(
        'SELECT 1 FROM education_submissions WHERE job_id=:job LIMIT 1'), {'job': job_id}).first():
        return True
    if printer_id is not None and db.execute(text(
        'SELECT 1 FROM education_submissions s JOIN jobs j ON j.id=s.job_id '
        "WHERE j.printer_id=:printer AND j.status IN ('printing','paused','scheduled') LIMIT 1"),
        {'printer': printer_id}).first():
        return True
    return False


def safe_copy(value):
    """Preserve useful alert text while stripping embedded credential material."""
    from core.redaction import redact_urls
    value = redact_urls(value)
    value = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----',
                   '[redacted]', value, flags=re.S)
    return re.sub(r'(?i)\b((?:access[ _-]?code|api[ _-]?key|token|password|secret|authorization)'
                  r'\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+', r'\1[redacted]', value)
