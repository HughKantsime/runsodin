"""Match terminal history conservatively; malformed/unavailable history is optional."""
import math
from datetime import datetime, timezone

TERMINAL = {'completed': 'completed', 'cancelled': 'cancelled', 'error': 'failed',
            'klippy_shutdown': 'failed', 'klippy_disconnect': 'failed', 'interrupted': 'failed'}


# A pause can close a job as cancelled and open another for the same print, which carries on
# its counters (seen on a Kobra S1: 000007 cancelled, 000008 completed, 0.08 s apart).
CONTINUATION_SECONDS = 5


def _continues(previous, job):
    """The next job is the same print resumed: it opens as the last closes and keeps its clock."""
    return (previous['status'] == 'cancelled' and previous['total'] is not None and job['total'] is not None
            and 0 <= job['start'] - previous['end'] <= CONTINUATION_SECONDS
            and abs(job['total'] - (job['end'] - job['start']) - previous['total']) <= CONTINUATION_SECONDS)


def _prints(jobs):
    """Join each print's jobs, oldest first. Its last job holds its outcome and running totals."""
    prints = []
    for job in sorted(jobs, key=lambda item: item['start']):
        if prints and _continues(prints[-1], job):
            prints[-1] = {**job, 'start': prints[-1]['start']}
        else:
            prints.append(job)
    return prints


def terminal_history(printer, filename, observed_end, *, row_started_at=None):
    """The one print that ended now, or, given a row's start, the one print that row watched.

    An end first seen after downtime can be any age, so only its row can place it.
    """
    try:
        jobs = []
        for job in printer.get_job_history(limit=20):
            if job.get('filename') != filename:
                continue
            if job.get('status') == 'in_progress':
                return None  # Not settled: an earlier job of this print cannot speak for its end.
            if job.get('status') not in TERMINAL:
                continue
            end = float(job['end_time'])
            start = float(job['start_time'])
            duration = job.get('print_duration')
            if duration is None:
                duration = job.get('total_duration')
            duration = float(duration)
            total = job.get('total_duration')
            total = None if total is None else float(total)
            if (not all(math.isfinite(v) for v in (start, end, duration))
                    or start > end or duration < 0 or (total is not None and not math.isfinite(total))):
                continue
            jobs.append({'status': job['status'], 'start': start, 'end': end, 'duration': duration, 'total': total})
        candidates = []
        for run in _prints(jobs):
            matches = (abs(observed_end - run['end']) <= 300 if row_started_at is None
                       else run['start'] - 120 <= row_started_at <= run['end'])
            if not matches:
                continue
            candidates.append({'status': TERMINAL[run['status']],
                'started_at': datetime.fromtimestamp(run['start'], timezone.utc).isoformat(),
                'ended_at': datetime.fromtimestamp(run['end'], timezone.utc).isoformat(),
                'duration': run['duration']})
        return candidates[0] if len(candidates) == 1 else None
    except Exception:
        return None
