"""Public read-only handle for a durable dispatch attempt."""
def status_url(job_id, token):
    return f'/api/jobs/{job_id}/dispatch-status?attempt_id={token}'
