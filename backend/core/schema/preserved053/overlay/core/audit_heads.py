"""Advance continuity after breaks, preserving evidence until an explicit acceptance."""
from core.audit_keys import saved_head, save_head
from core.audit_receipts import authentic


def continuity(item):
    return item['reason'].startswith(('signed_head', 'status_'))


def advance_head(result, *, accepted=None):
    try:
        prior = saved_head()
    except (ValueError, OSError):
        prior = None
    trusted = prior if authentic(prior) else {}
    observed = set(trusted.get('observed_breaks', []))
    observed.update(b['fingerprint'] for b in result['breaks'])
    value = {'head': result['head'], 'rows': result['rows'],
             'checked_at': result['checked_at'], 'after_break': len(observed),
             'observed_breaks': sorted(observed),
             'continuity_breaks': [b for b in result['breaks'] if continuity(b)] if accepted is None else [],
             'accepted_restore_baseline': accepted,
             'pending_restore': result.get('pending_restore') if accepted is None else None}
    save_head(value)
