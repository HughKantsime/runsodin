"""Native notification categories, safe copy, and Education delivery boundary."""
import re
from core.recipient_policy import education_notification_scope as education_scope

CATEGORIES = {
    'print_complete': 'PRINT_COMPLETE', 'print_failed': 'PRINT_FAILED',
    'print_paused': 'PRINT_PAUSED', 'print_cancelled': 'PRINT_CANCELLED',
    'printer_error': 'HMS_ERROR', 'spaghetti_detected': 'SPAGHETTI_DETECTED',
    'detachment_detected': 'SPAGHETTI_DETECTED', 'first_layer_issue': 'FIRST_LAYER_ISSUE',
    'spool_low': 'SPOOL_LOW', 'job_submitted': 'JOB_APPROVAL_REQUIRED',
    'printer_offline': 'PRINTER_OFFLINE',
}



def safe_copy(value):
    """Preserve useful alert text while stripping embedded credential material."""
    from core.redaction import redact_urls
    value = redact_urls(value)
    value = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----',
                   '[redacted]', value, flags=re.S)
    return re.sub(r'(?i)\b((?:access[ _-]?code|api[ _-]?key|token|password|secret|authorization)'
                  r'\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+', r'\1[redacted]', value)
