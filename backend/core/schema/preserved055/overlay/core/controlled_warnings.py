"""Rate-limited fail-closed diagnostics without payloads, credentials or error text."""
import logging
import threading
import time

_last = {}
_lock = threading.Lock()
log = logging.getLogger('odin.controlled')


def warn_failure(surface, error):
    now = time.monotonic()
    with _lock:
        if now - _last.get(surface, float('-inf')) < 60:
            return
        _last[surface] = now
    log.warning('Controlled check failed; %s withheld (%s)', surface, type(error).__name__)
