"""License admission in the post-upload gate chain before a print command (B2a).

Wraps the existing gate chain (maintenance, AMS, capability). The license
admission runs first, then the existing gates, so a wait for the policy lock
never ages their freshness checks (they stay closest to the send). If an
existing gate then refuses, the admission is withdrawn. The gate never raises:
a refusal is recorded so dispatch treats it as a clean no-start.
"""
from threading import Lock


def create_license_gate(engine, *, job, claim_token, previous=None):
    """Return the composed gate, or ``previous`` unchanged while rollout is off."""
    from core import license_lock
    if not license_lock.enabled():
        return previous
    from modules.printers.dispatch_ams_gate import record_refusal
    lock = Lock()
    used = False

    def _prior_refused(callback):
        refusal = getattr(previous, 'refusal', None)
        return record_refusal(checked, getattr(refusal, 'code', 'gate_rejected'),
                              getattr(refusal, 'detail', 'Post-upload callback refused print start'))

    def prepare():
        callback = getattr(previous, 'prepare', None) if previous is not None else None
        try:
            if callback is not None and callback() is not True:
                return _prior_refused(callback)
        except Exception as exc:
            return record_refusal(checked, 'exception:' + type(exc).__name__, 'Post-upload preparation raised an exception')
        return True

    def checked():
        nonlocal used
        with lock:
            if used:
                return record_refusal(checked, 'already_used', 'License start gate is single use')
            used = True
        refused = license_lock.admit_start(engine, printer_id=job['printer_id'], job_id=job['id'],
                                           claim_token=claim_token)
        if refused is not None:
            detail = refused.detail if isinstance(getattr(refused, 'detail', None), dict) else {}
            return record_refusal(checked, detail.get('code', 'license_policy_unavailable'),
                                  license_lock.refusal_message(refused, outcome=False))
        try:
            if previous is None or previous() is True:
                return True
            result = _prior_refused(previous)
        except Exception as exc:
            result = record_refusal(checked, 'exception:' + type(exc).__name__, 'Post-upload gate raised an exception')
        license_lock.withdraw_admission(engine, claim_token)
        return result

    checked.refusal = None
    checked.prepare = prepare
    return checked
