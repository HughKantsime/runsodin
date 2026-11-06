"""External signed-head continuity and stable identities for observed breaks."""
import hashlib
import json
from sqlalchemy import text
from core.audit_integrity import GENESIS
from core.audit_receipts import authentic, encoded
from core.audit_keys import saved_head


def identify(item):
    return hashlib.sha256(encoded({k: v for k, v in item.items() if k not in ('known', 'fingerprint')})).hexdigest()


def prior_evidence(connection, broken):
    try:
        anchor = saved_head()
    except (OSError, ValueError):
        broken('signed_head_unavailable')
        anchor = None
    if anchor is not None and not authentic(anchor):
        broken('signed_head_unauthentic')
        anchor = None
    raw = connection.execute(text('SELECT checked_at,result FROM audit_integrity_status WHERE id=1')).first()
    try:
        prior = json.loads(raw[1]) if raw else None
        if prior is not None and (not authentic(prior) or prior.get('checked_at') != raw[0]):
            broken('status_unauthentic')
            prior = None
        elif prior is None and anchor is not None:
            broken('status_missing')
    except (ValueError, TypeError):
        broken('status_unauthentic')
        prior = None
    if anchor is None and prior is not None:
        broken('signed_head_unavailable')
        # Still compare the authenticated database cursor; missing external
        # continuity evidence must never silently disable the head check.
        anchor = prior
    return prior, anchor


def check_anchor(connection, anchor, checkpoints, broken):
    if anchor is None:
        return
    seq, digest = anchor['rows'], anchor['head']
    if seq == 0 and digest == GENESIS:
        return
    actual = connection.execute(text('SELECT row_hash FROM audit_logs WHERE chain_seq=:seq'), {'seq': seq}).scalars().all()
    if actual == [digest]:
        return
    for checkpoint in checkpoints.values():
        if authentic(checkpoint) and checkpoint.get('anchors', {}).get(str(seq)) == digest:
            return
    broken('signed_head_missing', chain_seq=seq, head=digest)
