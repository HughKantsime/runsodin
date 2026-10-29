"""Match terminal history conservatively; malformed/unavailable history is optional."""
import math
from datetime import datetime, timezone

TERMINAL = {'completed': 'completed', 'cancelled': 'cancelled', 'error': 'failed',
            'klippy_shutdown': 'failed', 'klippy_disconnect': 'failed', 'interrupted': 'failed'}


def terminal_history(printer, filename, observed_end):
    try:
        candidates = []
        for job in printer.get_job_history(limit=20):
            if job.get('filename') != filename or job.get('status') not in TERMINAL:
                continue
            end = float(job['end_time'])
            start = float(job['start_time'])
            duration = job.get('print_duration')
            if duration is None:
                duration = job.get('total_duration')
            duration = float(duration)
            if (not all(math.isfinite(v) for v in (start, end, duration))
                    or start > end or duration < 0 or abs(observed_end - end) > 300):
                continue
            candidates.append({'status': TERMINAL[job['status']],
                'started_at': datetime.fromtimestamp(start, timezone.utc).isoformat(),
                'ended_at': datetime.fromtimestamp(end, timezone.utc).isoformat(),
                'duration': duration})
        return candidates[0] if len(candidates) == 1 else None
    except Exception:
        return None
