"""Snapshot chain walker and constant-time target-row redaction verification."""
import json
from sqlalchemy import text
from core.audit_integrity import GENESIS, projection
from core.audit_receipts import authentic


def accepted(connection, row, *, recorded=False):
    if row['hash_salt'] is not None and row['row_hash'] == row['content_hash']:
        return True
    evidence = connection.execute(text('SELECT evidence,record_id FROM audit_redactions WHERE row_id=:id'),
                                  {'id': row['id']}).first()
    if evidence is None:
        return False
    try:
        d = json.loads(evidence[0])
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- projection uses fixed FIELDS, a. alias and dialect hash expressions; evidence row id is bound
        record = connection.execute(text(f'SELECT {projection(connection)} FROM audit_logs a WHERE id=:id'),
                                    {'id': evidence[1]}).mappings().first()
        return (authentic(d) and d['row_id'] == row['id'] and d['record_id'] == evidence[1]
                and (recorded or d['before_valid']) and d['pre_erasure_row_hash'] == row['row_hash']
                and d['after_content_hash'] == row['content_hash'] and d['stable_hash'] == row['stable_hash']
                and row['hash_salt'] is None and record is not None and record['action'] == 'audit.erasure'
                and record['chain_seq'] > row['chain_seq'] and record['row_hash'] == record['content_hash']
                and json.loads(record['details'])['operation'] == d['operation'])
    except (ValueError, KeyError, TypeError):
        return False


def walk(connection, start=None):
    from core.audit_keys import missing_key
    key_missing = missing_key(connection)
    from core.audit_verify_evidence import prior_evidence, check_anchor, identify
    state = connection.execute(text('SELECT * FROM audit_chain_state WHERE id=1')).mappings().first()
    breaks, checkpoints = [], {}

    def broken(reason, row_id=None, *, known=False, **evidence):
        item = {'reason': reason, 'row_id': row_id, 'known': known, **evidence}
        item['fingerprint'] = identify(item)
        breaks.append(item)

    if state is None or state['genesis'] != GENESIS:
        broken('genesis')
    prior, anchor = (None, None) if key_missing else prior_evidence(connection, broken)
    for first, raw in connection.execute(text('SELECT first_seq,evidence FROM audit_retention_checkpoints ORDER BY first_seq')):
        try:
            d = json.loads(raw)
            if not isinstance(d, dict):
                raise ValueError()
            unsealed = d.get('unsealed') is True and d.get('reason') == 'audit_key_missing'
            if (not authentic(d) and not unsealed and not key_missing) or d['first_seq'] != first or not 0 < first <= d['last_seq']:
                raise ValueError()
            if any(not isinstance(d[name], str) or len(d[name]) != 64
                   for name in ('prev_hash', 'last_hash')):
                raise ValueError()
            if unsealed:
                broken('unverified_deletion', first_seq=first, last_seq=d['last_seq'],
                       unsealed=unsealed, category='deletion',
                       message="Unverified deletion: can't be told apart from tampering")
            checkpoints[first] = d
            if not d.get('before_valid') and authentic(d):
                broken('checkpoint_content', known=True, first_seq=first, last_seq=d['last_seq'])
        except (ValueError, KeyError, TypeError):
            broken('checkpoint', first_seq=first, evidence=raw)
    # NULL/zero/negative sequences are never silently excluded, even on incremental walks.
    for row_id, seq in connection.execute(text('SELECT id,chain_seq FROM audit_logs WHERE chain_seq IS NULL OR chain_seq<=0 ORDER BY id')):
        broken('unchained', row_id, chain_seq=seq)
    previous, sequence = (start['head'], start['rows']) if start else (GENESIS, 0)
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- projection uses fixed FIELDS, a. alias and dialect hash expressions; starting sequence is bound
    rows = connection.execute(text(f'SELECT {projection(connection)} FROM audit_logs a WHERE chain_seq>:seq ORDER BY chain_seq,id')
                              .execution_options(stream_results=True, max_row_buffer=256), {'seq': sequence}).mappings()
    used = set()

    def skip_deleted(previous, sequence):
        while sequence+1 in checkpoints and sequence+1 not in used:
            d = checkpoints[sequence+1]
            used.add(sequence+1)
            if d['prev_hash'] != previous:
                broken('checkpoint_link', first_seq=d['first_seq'], prev_hash=d['prev_hash'], expected=previous)
            previous, sequence = d['last_hash'], d['last_seq']
        return previous, sequence

    count = 0
    for row in rows:
        previous, sequence = skip_deleted(previous, sequence)
        if row['chain_seq'] != sequence+1 or row['prev_hash'] != previous:
            broken('link', row['id'], chain_seq=row['chain_seq'], prev_hash=row['prev_hash'], expected=previous)
        if not accepted(connection, row):
            receipt = connection.execute(text('SELECT evidence FROM audit_redactions WHERE row_id=:id'), {'id': row['id']}).scalar()
            try:
                unsealed = json.loads(receipt or '{}').get('unsealed') is True
            except (ValueError, TypeError, AttributeError):
                unsealed = False
            if unsealed:
                broken('unverified_redaction', row['id'], unsealed=unsealed, category='content',
                       message="Unverified redaction: can't be told apart from tampering",
                       content_hash=row['content_hash'], row_hash=row['row_hash'])
            elif not (key_missing and receipt):
                # Signed evidence cannot be judged while its key is unavailable.
                # The installation-level audit_key_missing state remains a refusal.
                broken('content', row['id'], known=accepted(connection, row, recorded=True),
                       content_hash=row['content_hash'], row_hash=row['row_hash'])
        previous, sequence = row['row_hash'], row['chain_seq']
        count += 1
    previous, sequence = skip_deleted(previous, sequence)
    if not start:
        retained = sum(d['last_seq']-d['first_seq']+1 for first, d in checkpoints.items() if first in used)
        if state is not None and count+retained != state['head_seq']:
            broken('row_count', actual=count, retained=retained, expected=state['head_seq'])
        for first in checkpoints.keys()-used:
            broken('checkpoint_overlap', first_seq=first)
    if state is None or sequence != state['head_seq'] or previous != state['head_hash']:
        broken('head', chain_seq=sequence, head=previous)
    check_anchor(connection, anchor, checkpoints, broken)
    # Carry unresolved continuity failures while still checking each NEW head.
    # Accepting a restore baseline acknowledges only these historical failures.
    for item in (anchor or {}).get('continuity_breaks', []):
        if item['fingerprint'] not in {b['fingerprint'] for b in breaks}:
            breaks.append({**item, 'known': True})
    known = {b.get('fingerprint') for b in prior.get('breaks', [])} if prior else set()
    for item in breaks:
        item['known'] = item['known'] or item['fingerprint'] in known
    first = next((b for b in breaks if b['row_id'] is not None), breaks[0] if breaks else None)
    from core.audit_restore_markers import pending_restore
    pending = pending_restore(connection, anchor)
    return {**({'status': 'audit_key_missing'} if key_missing else {}),
            'pending_restore': pending, 'ok': not breaks and not key_missing,
            'first_break': first['row_id'] if first else None,
            'reason': 'audit_key_missing' if key_missing else first['reason'] if first else None, 'breaks': breaks,
            'head': previous, 'rows': sequence, 'genesis': GENESIS}
