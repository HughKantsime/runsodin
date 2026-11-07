"""Bounded independently sealed head history; only the new head is signed per save."""
import hashlib
from core.audit_receipts import encoded

# Keep immediate transitions, daily heads for 90 days, then weekly heads.
# The hard cap applies to routine heads. Acceptance evidence is never discarded.
MAX_HISTORY = 210


def history_digest(history):
    return hashlib.sha256(encoded(history)).hexdigest()


def compact(history, now):
    acceptances = [entry for entry in history if entry.get('accepted_restore_baseline')]
    routine = [entry for entry in history if not entry.get('accepted_restore_baseline')]
    recent, older = routine[-16:], routine[:-16]
    buckets = {}
    for entry in older:
        checked = entry.get('checked_at', 0)
        period = 86400 if now-checked < 90*86400 else 7*86400
        buckets[(period, int(checked//period))] = entry
    return sorted(acceptances + (list(buckets.values()) + recent)[-MAX_HISTORY:],
                  key=lambda entry: entry.get('checked_at', 0))


def persist(value):
    from core.audit_keys import saved_head, directory, atomic_json
    from core.audit_receipts import seal, authentic
    try:
        prior = saved_head()
    except (ValueError, OSError):
        prior = {'unreadable_head_sha256': hashlib.sha256(
            (directory()/'.audit-signed-head.json').read_bytes()).hexdigest()}
    trusted = authentic(prior)
    history = list(prior.get('history', [])) if trusted else []
    if trusted:
        # Store one compact independent receipt; never sign the whole accumulated
        # history or duplicate growing break lists inside every historical entry.
        history.append(seal({k: prior[k] for k in (
            'head', 'rows', 'checked_at', 'after_break', 'accepted_restore_baseline') if k in prior}))
    elif prior:
        history.append(seal({'untrusted_prior': {
            'unreadable_head_sha256': prior.get('unreadable_head_sha256', history_digest(prior))},
            'checked_at': value['checked_at']}))
    history = compact(history, value['checked_at'])
    head = seal({**value, 'head_signature_version': 2, 'history_sha256': history_digest(history)})
    atomic_json(directory()/'.audit-signed-head.json', {**head, 'history': history})
