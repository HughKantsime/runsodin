"""Install-wide license policy. Dormant until ODIN_LICENSE_FLEET_LOCK_ENABLED.

B1 owns evidence, selections and user authorization. B2a adds printer checks and
the final print-start admission (admit_start). Physical-attempt capture (exempt
prints on newly locked printers) is B2b; until then only members are allowed N.
"""
from dataclasses import dataclass
import hashlib
import json
import os
import re
import time
from types import MappingProxyType
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import text

POLICY_KEY = 'license_active_selection'
EVIDENCE_KEY = 'license_lock_evidence'
REVISION_KEY = 'license_lock_revision'
# Print starts admitted under the policy lock, keyed by a digest of the dispatch
# claim token (never the token). B2b turns a live entry into the exempt attempt
# when its printer leaves the selection.
ADMITTED_KEY = 'license_admitted_attempts'


def enabled():
    return os.getenv('ODIN_LICENSE_FLEET_LOCK_ENABLED', '').lower() in {'1', 'true', 'yes', 'on'}


def paid_lapse_enabled():
    # Shane 2026-11-08: a paid lapse gets the same lock as a trial lapse. On by
    # default; the variable remains only as an explicit off switch.
    return os.getenv('ODIN_LICENSE_PAID_LAPSE_ENABLED', '').lower() not in {'0', 'false', 'no', 'off'}


# Deliberately no restricted-session issuer in B1. An environment flag alone
# cannot safely enable a scope whose routes and operator decision are pending.
RESTRICTED_RECOVERY_ENABLED = False
COMMUNITY_DOWNGRADE_ENABLED = False


class LicenseLockError(HTTPException):
    def __init__(self, code, *, status=403, revision=0, resource_id=None, reason=None, resource_type='user'):
        detail = dict(code=code, message=code.replace('_', ' '), resource_type=resource_type,
                      policy_revision=revision, reason=reason or code)
        if resource_id is not None:
            detail['resource_id'] = resource_id
        super().__init__(status, detail, headers={'X-ODIN-License-Lock': code})


def _decode(value):
    return json.loads(value) if isinstance(value, str) else value


def _read(db, key):
    value = db.execute(text('SELECT value FROM system_config WHERE key=:key'), {'key': key}).scalar()
    return _decode(value) if value is not None else None


def _write(db, key, value):
    db.execute(text('INSERT INTO system_config (key,value) VALUES (:key,:value) '
                    'ON CONFLICT(key) DO UPDATE SET value=excluded.value'),
               {'key': key, 'value': json.dumps(value, separators=(',', ':'))})


def _lock(db):
    dialect = db.get_bind().dialect.name
    if dialect == 'sqlite':
        conn = db.connection().connection.driver_connection
        if not conn.in_transaction:
            db.execute(text('BEGIN IMMEDIATE'))
    else:
        db.execute(text("SET LOCAL lock_timeout = '2s'"))
    db.execute(text("INSERT INTO system_config (key,value) VALUES (:key,'null') ON CONFLICT(key) DO NOTHING"), {'key': POLICY_KEY})
    # PostgreSQL row lock, SQLite writer lock, held until caller commits.
    db.execute(text('UPDATE system_config SET value=value WHERE key=:key'), {'key': POLICY_KEY})


def _next_revision(db, previous):
    try:
        durable = _read(db, REVISION_KEY)
    except (ValueError, TypeError):
        durable = 0
    value = max(previous, durable if type(durable) is int else 0) + 1
    _write(db, REVISION_KEY, value)
    return value


def _valid_ids(ids):
    return isinstance(ids, list) and all(type(i) is int and i > 0 for i in ids) and len(set(ids)) == len(ids)


def _valid_policy(p):
    fields = {'schema_version', 'revision', 'printers', 'users', 'membership_generation',
              'entitlement', 'recovery_user_id', 'exempt_attempts', 'transition_id',
              'effective_at', 'set_by', 'set_at', 'change_reason', 'usage', 'warning'}
    if not (isinstance(p, dict) and set(p) <= fields and p.get('schema_version') == 2
            and type(p.get('revision')) is int and p['revision'] >= 0
            and _valid_ids(p.get('printers')) and _valid_ids(p.get('users'))
            and isinstance(p.get('entitlement'), dict)
            and type(p.get('membership_generation')) is int and p['membership_generation'] >= 0):
        return False
    e = p['entitlement']
    return (set(e) <= {'state', 'digest', 'binding_digest', 'max_printers', 'max_users', 'enforce', 'reason', 'deadline', 'proven', 'lease_deadline', 'recovery_exception'}
            and e.get('state') in {'verified_current', 'verified_expired', 'community_initial', 'community_selected', 'verification_unavailable'}
            and type(e.get('enforce')) is bool
            and type(e.get('proven', False)) is bool
            and type(e.get('recovery_exception', False)) is bool
            and all(e.get(k) is None or type(e[k]) is int and e[k] >= 0 for k in ('max_users', 'max_printers'))
            and (p.get('recovery_user_id') is None or type(p['recovery_user_id']) is int and p['recovery_user_id'] > 0)
            and isinstance(p.get('exempt_attempts'), dict))


def eligible_local(row):
    # A syntactically valid bcrypt verifier, not an SSO/random placeholder.
    return (bool(row['is_active']) and row['role'] == 'admin' and row['group_id'] is None
            and isinstance(row['password_hash'], str)
            and re.fullmatch(r'\$2[aby]\$(?:0[4-9]|[12][0-9]|3[01])\$[./A-Za-z0-9]{53}', row['password_hash']) is not None)


def _members(db):
    printers = [r[0] for r in db.execute(text('SELECT id FROM printers WHERE is_active IS TRUE ORDER BY id'))]
    rows = db.execute(text('SELECT id,role,group_id,is_active,password_hash FROM users ORDER BY id')).mappings().all()
    users = [r['id'] for r in rows if r['is_active']]
    recovery = [r['id'] for r in rows if eligible_local(r)]
    return printers, users, recovery


def _fill(preferred, active, cap, recovery=None):
    active_set = set(active)
    result = [i for i in preferred if i in active_set]
    if cap == 0:
        return []
    if recovery is not None and recovery not in result:
        result.insert(0, recovery)
    existing = set(result)
    result.extend(i for i in active if i not in existing)
    if cap is not None:
        if recovery is not None and recovery not in result[:cap]:
            result.remove(recovery)
            result.insert(0, recovery)
        result = result[:cap]
    return result


def _entitlement(info):
    state = info.verification_state
    enforce = state == 'verified_current'
    caps = [info.signed_max_printers, info.signed_max_users]
    reason = info.reason_code
    if state == 'verified_expired':
        enforce = info.signed_trial or paid_lapse_enabled()
        caps = [5, 1]
        if not enforce:
            reason = 'paid_lapse_decision_pending'
    if state == 'community_selected':
        enforce = COMMUNITY_DOWNGRADE_ENABLED
        caps = [5, 1]
    return dict(state=state, digest=info.verified_digest, binding_digest=info.binding_digest,
                max_printers=caps[0], max_users=caps[1], enforce=enforce, reason=reason,
                recovery_exception=caps[1] == 0,
                deadline=info.expiry_deadline.isoformat() if info.expiry_deadline else None)


def _frozen_members(previous, active, entitlement, cap_key, recovery=None, old_recovery=None, users=False):
    """Membership while unverifiable: the last proven members that are still active.

    Never adds a member except to fill a vacancy: within caps proven by the saved
    signed envelope, or (unproven) only by the recovery admin taking a vacated seat.
    Cached caps alone never change who is locked.
    """
    active_set = set(active)
    kept = [i for i in previous if i in active_set]
    if (users and old_recovery is not None and recovery is not None and old_recovery != recovery
            and old_recovery in kept and recovery not in kept):
        # Lost recovery eligibility hands that seat to the successor; if the successor
        # already holds a seat, nobody's seat changes.
        kept[kept.index(old_recovery)] = recovery
    if users and recovery is None and entitlement.get('enforce') and entitlement.get(cap_key) is not None:
        # Same guard as verified reconciliation: no seat refills without a recovery admin.
        return kept
    if not entitlement.get('proven'):
        if recovery is not None and recovery not in kept and len(kept) < len(previous):
            kept.insert(0, recovery)  # the recovery admin takes a vacated seat only
        return kept
    return _fill(kept, active, entitlement.get(cap_key), recovery)


def _evidence(db, info, previous):
    """Persist only verified envelopes; while unverifiable, freeze the last proven entitlement."""
    from license_manager import license_clock
    from core.license_clock import instant
    now, _uncertain = license_clock().sample()
    try:
        evidence = _read(db, EVIDENCE_KEY)
    except (ValueError, TypeError):
        evidence = None
    if info.verification_state in {'verified_current', 'verified_expired'}:
        new = dict(schema_version=1, signed_envelope=info.signed_envelope, sha256=info.verified_digest,
                   binding_digest=info.binding_digest, accepted_at=now.isoformat(), time_anchor=now.isoformat(),
                   verification_state=info.verification_state,
                   uncertainty_started_at=None)
        try:
            elapsed = (now - instant(evidence['time_anchor'])).total_seconds()
            checkpoint_due = elapsed < 0 or elapsed >= 300
        except (TypeError, KeyError, ValueError):
            checkpoint_due = True
        if not evidence or checkpoint_due or any(new[k] != evidence.get(k) for k in ('sha256', 'binding_digest', 'verification_state')) or evidence.get('uncertainty_started_at'):
            _write(db, EVIDENCE_KEY, new)
        return _entitlement(info), None
    if info.verification_state == 'community_selected' or (info.verification_state == 'community_initial' and isinstance(evidence, dict) and evidence.get('community_selected')):
        return dict(_entitlement(info), state='community_selected', enforce=False, reason='community_downgrade_decision_pending'), None
    if info.verification_state == 'community_initial' and not evidence and not (previous or {}).get('enforce'):
        return _entitlement(info), None
    warning = 'license_verification_unavailable'
    # Shane 2026-11-08 ("stay locked", clarified "freeze last-good"): while the
    # license cannot be verified, keep the last proven state with no time limit.
    # Uncertainty never adds or removes locks; only fresh verification or a known
    # expiry changes the state. _reconcile freezes membership for 'uncertainty_frozen'.
    if isinstance(evidence, dict) and evidence.get('schema_version') == 1 and (
            not evidence.get('uncertainty_started_at') or 'uncertainty_deadline' in evidence):
        evidence['uncertainty_started_at'] = evidence.get('uncertainty_started_at') or now.isoformat()
        evidence.pop('uncertainty_deadline', None)
        _write(db, EVIDENCE_KEY, evidence)
    proof = _saved_proof(evidence)
    if proof is not None and proof.verification_state == 'verified_expired':
        # Known expiry from the saved signed envelope on a trusted clock.
        return dict(_entitlement(proof), proven=True), warning
    proven_current = proof is not None and proof.verification_state == 'verified_current'
    if previous and previous.get('state'):
        frozen = dict(previous, state='verification_unavailable', reason='uncertainty_frozen', proven=proven_current)
        frozen.pop('lease_deadline', None)
        if proven_current:
            # Caps come only from the reverified signed envelope, never the stored row.
            if (previous.get('max_printers'), previous.get('max_users')) != (proof.signed_max_printers, proof.signed_max_users):
                warning = 'license_policy_evidence_mismatch'
            frozen.update(max_printers=proof.signed_max_printers, max_users=proof.signed_max_users,
                          recovery_exception=proof.signed_max_users == 0)
        else:
            # Unproven: keep the last-good recovery authorization as recorded, never
            # re-derive it from cached caps.
            if 'recovery_exception' in previous:
                frozen['recovery_exception'] = previous['recovery_exception'] is True
            else:
                # Migration: policies saved before the flag existed were authorized by the
                # rule they were saved under (a zero seat cap). Keep that last-good
                # authorization once; the frozen row then carries the explicit flag.
                frozen['recovery_exception'] = previous.get('max_users') == 0
        return frozen, warning
    if proven_current:
        # Lost or corrupt policy row with usable evidence: rebuild from the proof.
        return dict(_entitlement(proof), state='verification_unavailable', reason='policy_rebuilt_from_evidence', proven=True), warning
    # Nothing was ever proven here: Slice A/Community semantics, as before.
    return dict(_entitlement(info), state='verification_unavailable', enforce=False), warning


def _saved_proof(evidence):
    """The saved signed envelope, reverified now against the current binding, or None."""
    from license_manager import verify_license_text
    raw = evidence.get('signed_envelope') if isinstance(evidence, dict) else None
    if not isinstance(raw, str) or hashlib.sha256(raw.encode()).hexdigest() != evidence.get('sha256'):
        return None
    verified = verify_license_text(raw)
    if verified.verified_digest != evidence.get('sha256') or verified.binding_digest != evidence.get('binding_digest'):
        return None
    return verified


def _reconcile(db, *, selection=None, expected_revision=None, actor=None, recovery_user_id=None):
    """One short caller-owned transaction; never commits unrelated work."""
    from license_manager import get_license, license_clock
    _lock(db)
    try:
        old = _read(db, POLICY_KEY)
    except (ValueError, TypeError):
        old = None
    valid = _valid_policy(old)
    revision = old.get('revision', 0) if isinstance(old, dict) and type(old.get('revision')) is int else 0
    try:
        durable_revision = _read(db, REVISION_KEY)
    except (ValueError, TypeError):
        durable_revision = 0
    revision = max(revision, durable_revision if type(durable_revision) is int else 0)
    if expected_revision is not None and expected_revision != revision:
        raise LicenseLockError('license_selection_stale', status=409, revision=revision)
    if not valid:
        old = dict(schema_version=2, revision=revision, printers=[], users=[], membership_generation=0,
                   entitlement={}, recovery_user_id=None, exempt_attempts={}, transition_id=None,
                   effective_at=None, set_by=None, set_at=None, change_reason='initial')
    entitlement, warning = _evidence(db, get_license(), old['entitlement'])
    printers, users, eligible = _members(db)
    recovery = old.get('recovery_user_id')
    if recovery_user_id is not None:
        if type(recovery_user_id) is not int or recovery_user_id not in eligible:
            raise LicenseLockError('license_selection_invalid', status=422, revision=revision, reason='ineligible_recovery')
        recovery = recovery_user_id
    if recovery not in eligible:
        recovery = eligible[0] if eligible else None
    frozen = entitlement.get('reason') == 'uncertainty_frozen'
    if selection is not None and frozen and not entitlement.get('proven'):
        # No proven caps to validate a new selection against; keep the frozen one.
        raise LicenseLockError('license_verification_unavailable', status=409, revision=revision)
    picks = selection or {'printers': old['printers'], 'users': old['users']}
    if selection is not None:
        for name, active in [('printers', printers), ('users', users)]:
            ids, cap = picks.get(name), entitlement.get('max_' + name)
            if not _valid_ids(ids) or any(i not in active for i in ids) or (cap is not None and len(ids) > cap):
                raise LicenseLockError('license_selection_invalid', status=422, revision=revision)
        if entitlement.get('max_users') != 0 and recovery is not None and picks['users'] and recovery not in picks['users']:
            raise LicenseLockError('license_selection_invalid', status=422, revision=revision, reason='recovery_seat_required')
    if frozen and selection is None:
        member_printers = _frozen_members(old['printers'], printers, entitlement, 'max_printers')
        member_users = _frozen_members(old['users'], users, entitlement, 'max_users', recovery,
                                       old.get('recovery_user_id'), users=True)
    else:
        member_printers = _fill(picks['printers'], printers, entitlement.get('max_printers'))
        member_users = (_fill(picks['users'], users, entitlement.get('max_users'), recovery)
                        if recovery is not None or entitlement.get('max_users') is None or not entitlement.get('enforce') else [])
    resolved = dict(old, entitlement=entitlement, recovery_user_id=recovery,
                    printers=member_printers, users=member_users,
                    usage={'printers': len(printers), 'users': len(users)}, warning=warning)
    if not valid and revision:
        resolved['warning'] = 'license_policy_reconstructed'
    if recovery is None:
        resolved['warning'] = resolved['warning'] or 'local_recovery_bootstrap_required'
    if selection is not None:
        resolved.update(set_by=actor, set_at=license_clock().sample()[0].isoformat(), change_reason='explicit_selection')
    elif resolved != old:
        resolved['change_reason'] = 'automatic_reconciliation'
    if resolved != old or not valid:
        resolved['revision'] = _next_revision(db, revision)
        resolved['transition_id'] = str(uuid4())
        resolved['effective_at'] = license_clock().sample()[0].isoformat()
        _write(db, POLICY_KEY, resolved)
    return resolved


def reconcile(db, **kwargs):
    try:
        return _reconcile(db, **kwargs)
    except HTTPException:
        raise
    except Exception:
        raise LicenseLockError('license_policy_unavailable', status=503) from None


@dataclass(frozen=True)
class PolicySnapshot:
    revision: int
    enforcing: bool
    printers: frozenset
    users: frozenset
    recovery_user_id: int | None
    entitlement: object
    warning: str | None
    membership_generation: int
    transition_id: str | None = None
    effective_at: str | None = None
    exempt_attempts: object = None

    @property
    def deadline(self):
        return self.entitlement.get('deadline')

    @property
    def evidence_generation(self):
        return self.entitlement.get('digest')


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def _snapshot(p):
    return PolicySnapshot(p['revision'], enabled() and p['entitlement'].get('enforce', False),
                          frozenset(p['printers']), frozenset(p['users']), p['recovery_user_id'],
                          MappingProxyType(dict(p['entitlement'])), p.get('warning'), p['membership_generation'], p.get('transition_id'), p.get('effective_at'),
                          _freeze(p.get('exempt_attempts', {})))


# While unverifiable, the license file's signature never changes, so a cached
# snapshot would never re-check the saved envelope. Re-reconcile at the 30-second
# retry cadence, and immediately once a known deadline passes.
UNVERIFIED_CACHE_SECONDS = 30


def _cache_current(cached, p, now):
    from core.license_clock import instant
    entitlement = p.get('entitlement') or {}
    try:
        # Re-reconcile once when a deadline is crossed after this entry was cached;
        # a deadline already past when cached (a frozen lapsed policy) is throttled below.
        cached_at = cached[4] if len(cached) > 4 else None
        if (entitlement.get('deadline') and now >= instant(entitlement['deadline'])
                and (cached_at is None or cached_at < instant(entitlement['deadline']))):
            return False
    except (TypeError, ValueError):
        return False
    if entitlement.get('state') == 'verification_unavailable':
        return len(cached) > 3 and time.monotonic() - cached[3] < UNVERIFIED_CACHE_SECONDS
    return True


_DISABLED = PolicySnapshot(0, False, frozenset(), frozenset(), None, MappingProxyType({}), None, 0)
_cache = {}


def snapshot(db, *, request=None, cycle=None):
    if not enabled():
        return _DISABLED
    holder = request.state if request is not None else cycle
    if holder is not None and getattr(holder, 'license_policy', None) is not None:
        return holder.license_policy
    from license_manager import get_license, license_clock
    info = get_license()
    now, uncertain = license_clock().sample()
    try:
        try:
            p = _read(db, POLICY_KEY)
        except (ValueError, TypeError):
            p = None
        signature = (info.verification_state, info.verified_digest, info.binding_digest, paid_lapse_enabled())
        cache_key = str(db.get_bind().url)
        cached = _cache.get(cache_key)
        fresh = (cached and _valid_policy(p) and cached[0] == p['revision'] and cached[1] == signature
                 and _cache_current(cached, p, now))
        if fresh:
            result = cached[2]
        else:
            # Use an independent transaction only when the caller has not begun
            # writing. Issuance/mutations already own the serialization lock.
            connection = db.connection()
            writing = connection.info.get('license_membership_write', False)
            if connection.dialect.name == 'sqlite':
                writing = connection.connection.driver_connection.in_transaction
            if writing:
                p = reconcile(db)
            else:
                from sqlalchemy.orm import Session
                with Session(db.get_bind()) as policy_db:
                    p = reconcile(policy_db)
                    policy_db.commit()
            result = _snapshot(p)
            _cache[cache_key] = (p['revision'], signature, result, time.monotonic(), now)
        if holder is not None:
            holder.license_policy = result
        return result
    except HTTPException:
        raise
    except Exception:
        raise LicenseLockError('license_policy_unavailable', status=503) from None


def user_allowed(policy, user_id):
    return (not policy.enforcing or user_id in policy.users
            # Deliberate full-access recovery exception, including at zero seats.
            # Keep until Shane decides restricted recovery sessions (review item 1).
            # recovery_exception is set from proof (or kept from the last proven state
            # while frozen); cached caps never open it.
            or policy.entitlement.get('recovery_exception') is True
            and user_id is not None and user_id == policy.recovery_user_id)


def require_user(policy, user_id, purpose='access'):
    if purpose in {'logout', 'credential_repair', 'revoke', 'enrollment_cancel'}:
        return
    if not user_allowed(policy, user_id):
        raise LicenseLockError('license_user_locked', revision=policy.revision, resource_id=user_id,
                               reason=policy.entitlement.get('reason'))


# R: stored reads. N: new hardware work (dispatch, probes, writes, power on).
# M/C/V for an exempt attempt arrive with B2b; until then they are members-only.
PRINTER_CAPABILITIES = frozenset({'R', 'N', 'M', 'C', 'V'})


def printer_allowed(policy, printer_id, cap='N'):
    if cap not in PRINTER_CAPABILITIES:
        raise ValueError('unknown printer capability')
    return not policy.enforcing or cap == 'R' or printer_id in policy.printers


def require_printer(policy, printer_id, cap='N'):
    if not printer_allowed(policy, printer_id, cap):
        raise LicenseLockError('license_printer_locked', revision=policy.revision, resource_id=printer_id,
                               resource_type='printer', reason=policy.entitlement.get('reason'))


def printer_refusal(engine, printer_id, cap='N'):
    """Early, non-authoritative check before any power, readiness or upload work.

    Returns None or a LicenseLockError. admit_start remains the authority."""
    if not enabled():
        return None
    from sqlalchemy.orm import Session
    try:
        with Session(engine) as db:
            require_printer(snapshot(db), printer_id, cap)
        return None
    except LicenseLockError as exc:
        return exc
    except Exception:
        return LicenseLockError('license_policy_unavailable', status=503, resource_id=printer_id, resource_type='printer')


def refusal_message(exc, *, outcome=True):
    """Operator text for a printer refusal; nothing was sent."""
    detail = exc.detail if isinstance(getattr(exc, 'detail', None), dict) else {}
    code = detail.get('code', 'license_policy_unavailable')
    if code == 'license_printer_locked':
        text = f'This printer is not in the active license selection ({detail.get("reason", code)})'
    else:
        text = 'The license policy could not be checked; retry shortly'
    return text + ('; no command was sent' if outcome else '')


def _attempt_key(claim_token):
    return hashlib.sha256(('license-admission:' + claim_token).encode()).hexdigest()[:32]


def admit_start(engine, *, printer_id, job_id, claim_token):
    """Final license admission for one print command; None admits.

    Runs immediately before the command, under the policy lock that selection
    changes take, so either a change that locks this printer commits first and
    this refuses, or this admission commits first and is recorded for B2b to
    keep as the printer's exempt attempt. Fails closed: any error refuses.
    Returns None (admitted) or a LicenseLockError (refused; nothing was sent).
    """
    if not enabled():
        return None
    from sqlalchemy.orm import Session
    try:
        with Session(engine) as db:
            policy = _snapshot(_reconcile(db))
            if not printer_allowed(policy, printer_id, 'N'):
                db.commit()  # keep any reconciliation; the refusal itself writes nothing
                return LicenseLockError('license_printer_locked', revision=policy.revision, resource_id=printer_id,
                                        resource_type='printer', reason=policy.entitlement.get('reason'))
            try:
                entries = _read(db, ADMITTED_KEY)
            except (ValueError, TypeError):
                entries = None
            live = {_attempt_key(row[0]) for row in db.execute(text('SELECT token FROM printer_dispatch_claims'))}
            entries = {k: v for k, v in entries.items() if k in live} if isinstance(entries, dict) else {}
            from license_manager import license_clock
            entries[_attempt_key(claim_token)] = dict(printer_id=printer_id, job_id=job_id, revision=policy.revision,
                                                      admitted_at=license_clock().sample()[0].isoformat())
            _write(db, ADMITTED_KEY, entries)
            db.commit()
            return None
    except LicenseLockError as exc:
        return exc
    except Exception:
        return LicenseLockError('license_policy_unavailable', status=503, resource_id=printer_id, resource_type='printer')


def authorize_user(db, user_id, *, request=None, purpose='access', issuance=False):
    if not enabled():
        return
    try:
        policy = issuance_snapshot(db) if issuance else snapshot(db, request=request)
    except HTTPException:
        raise
    except Exception:
        raise LicenseLockError('license_policy_unavailable', status=503) from None
    require_user(policy, user_id, purpose)


def issuance_snapshot(db):
    """Stable issuances share the policy row on PostgreSQL; only changes reconcile.

    SQLite still needs its writer reservation for atomic session issuance, but
    does not rewrite the policy/evidence on each login or MFA step.
    """
    policy = snapshot(db)
    if db.get_bind().dialect.name == 'postgresql':
        db.execute(text("SET LOCAL lock_timeout = '2s'"))
        raw = db.execute(text('SELECT value FROM system_config WHERE key=:key FOR SHARE'),
                         {'key': POLICY_KEY}).scalar()
    else:
        conn = db.connection().connection.driver_connection
        if not conn.in_transaction:
            db.execute(text('BEGIN IMMEDIATE'))
        raw = db.execute(text('SELECT value FROM system_config WHERE key=:key'), {'key': POLICY_KEY}).scalar()
    p = _decode(raw) if raw else None
    if _valid_policy(p) and p['revision'] == policy.revision:
        return policy
    return _snapshot(reconcile(db))


def replace_selection(db, *, printers, users, expected_revision, actor, recovery_user_id=None):
    if type(expected_revision) is not int or expected_revision < 0:
        raise LicenseLockError('license_selection_invalid', status=422)
    result = reconcile(db, selection={'printers': printers, 'users': users}, expected_revision=expected_revision,
                       actor=actor, recovery_user_id=recovery_user_id)
    from core.audit import log_audit
    log_audit(db, 'license.selection', 'system', details={'revision': result['revision'], 'actor_id': actor})
    return result


def status_view(p):
    enforcing = enabled() and p['entitlement'].get('enforce', False)
    return dict(p, rollout_enabled=enabled(), enforcing=enforcing,
                allowance={kind: p['entitlement'].get('max_' + kind) for kind in ('printers', 'users')},
                selected_counts={kind: len(p[kind]) for kind in ('printers', 'users')},
                locked_counts={kind: max(0, p['usage'][kind] - len(p[kind])) if enforcing else 0 for kind in ('printers', 'users')},
                paid_lapse_enabled=paid_lapse_enabled(), restricted_recovery_enabled=RESTRICTED_RECOVERY_ENABLED,
                reconciliation='rollout_disabled' if not enabled() else 'B2_B3_pending')


def selection_status(db, *, read_only=False):
    if not read_only:
        return status_view(reconcile(db))
    p = _read(db, POLICY_KEY)
    if not _valid_policy(p):
        from license_manager import get_license
        printers, users, eligible = _members(db)
        entitlement = _entitlement(get_license(persist_clock=False))
        recovery = eligible[0] if eligible else None
        p = dict(schema_version=2, revision=0, membership_generation=0,
                 entitlement=entitlement, recovery_user_id=recovery,
                 printers=_fill([], printers, entitlement['max_printers']),
                 users=_fill([], users, entitlement['max_users'], recovery),
                 exempt_attempts={}, usage={'printers': len(printers), 'users': len(users)},
                 warning='license_policy_pending')
    return status_view(p)


def request_purpose(request):
    """Exact, method-bound exceptions; never trust a caller-supplied purpose."""
    if request is None:
        return 'access'
    path = request.scope.get('path', '')
    if path.startswith('/api/v1/'):
        path = path[7:]
    elif path.startswith('/api/'):
        path = path[4:]
    return {('POST', '/auth/logout'): 'logout',
            ('DELETE', '/auth/biometric-token'): 'revoke',
            ('POST', '/auth/mfa/enrollment/cancel'): 'enrollment_cancel',
            ('POST', '/auth/reset-password'): 'credential_repair'}.get((request.method, path), 'access')


def mark_community_selected(db, actor):
    """Durable intent, distinct from an accidentally missing file. Pending policy."""
    if not enabled():
        return
    _lock(db)
    p = reconcile(db)
    p['entitlement'].update(state='community_selected', enforce=False, reason='community_downgrade_decision_pending')
    p.update(revision=_next_revision(db, p['revision']), set_by=actor, change_reason='explicit_community')
    _write(db, POLICY_KEY, p)
    _write(db, EVIDENCE_KEY, {'schema_version': 1, 'community_selected': True})


def remove_installed_license(db, actor, action, *, details=None):
    """Journal intent durably, then quarantine and commit policy/audit together.

    A failed transaction restores the active file. A process crash after rename
    leaves the journal so an absent artifact is recognized as intentional.
    """
    import license_manager as lm
    from core.audit import log_audit
    journal = 'license_lock_removal_intent'
    original = lm._find_license_file()
    quarantined = None
    try:
        if enabled():
            _lock(db)
            _write(db, journal, {'community_selected': True})
            db.commit()
        mark_community_selected(db, actor)
        quarantined = lm.quarantine_installed_license()
        log_audit(db, action, 'system', details=details or {'quarantined': os.path.basename(quarantined or '')})
        if enabled():
            db.execute(text('DELETE FROM system_config WHERE key=:key'), {'key': journal})
        db.commit()
        lm._cached_license = None
        return quarantined
    except Exception as exc:
        db.rollback()
        if quarantined and original:
            os.replace(quarantined, original)
        lm._cached_license = None
        if enabled():
            try:
                db.execute(text('DELETE FROM system_config WHERE key=:key'), {'key': journal})
                db.commit()
            except Exception:
                db.rollback()
        if isinstance(exc, LicenseLockError):
            raise
        raise LicenseLockError('license_policy_unavailable', status=503) from None


def install_membership_hooks():
    """Serialize every SQLAlchemy user/printer membership mutation, including imports.

    Hooks cover both ORM and raw SQL used by runtime services. No schema/triggers
    or preserved migration source changes. Telemetry-only updates are excluded.
    """
    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    if event.contains(Engine, 'before_cursor_execute', _before_membership):
        return
    event.listen(Engine, 'before_cursor_execute', _before_membership)
    event.listen(Engine, 'after_cursor_execute', _after_membership)
    event.listen(Engine, 'commit', _check_recovery_commit)
    event.listen(Engine, 'rollback', _clear_membership)
    from sqlalchemy.pool import Pool
    event.listen(Pool, 'checkin', _clear_membership_pool)


def _before_membership(conn, cursor, statement, parameters, context, executemany):
    if not enabled() or conn.info.get('license_hook'):
        return
    from core.sql_write_shape import WRITE as _WRITE, table_name as _table_name, set_columns as _set_columns, COMMENT as _COMMENT
    match = _WRITE.search(_COMMENT.sub(' ', statement))
    if not match or _table_name(match.group(2)) not in {'users', 'printers'}:
        return
    table = _table_name(match.group(2))
    if match.group(1).upper().startswith('UPDATE'):
        cols = _set_columns(statement, match.end())
        relevant = {'id', 'is_active', 'role', 'group_id', 'password_hash', 'username',
                    'oidc_issuer', 'oidc_subject', 'oidc_provider', 'mfa_enabled'} if table == 'users' else {
            'id', 'is_active', 'serial_number', 'ip_address', 'api_type', 'api_host', 'api_key',
            'access_code', 'hostname', 'machine_type', 'camera_url', 'camera_enabled',
            'plug_type', 'plug_host', 'plug_entity_id', 'plug_auth_token', 'org_id', 'shared'}
        if cols and '*' not in cols and not cols & relevant:
            return
        if not cols and table != 'users':
            return
    conn.info['license_hook'] = True
    try:
        if conn.dialect.name == 'sqlite' and not conn.connection.driver_connection.in_transaction:
            conn.exec_driver_sql('BEGIN IMMEDIATE')
        if conn.dialect.name == 'postgresql':
            conn.execute(text("SET LOCAL lock_timeout = '2s'"))
        conn.execute(text("INSERT INTO system_config (key,value) VALUES (:k,'null') ON CONFLICT(key) DO NOTHING"), {'k': POLICY_KEY})
        conn.execute(text('UPDATE system_config SET value=value WHERE key=:k'), {'k': POLICY_KEY})
        if 'license_had_recovery' not in conn.info:
            rows = conn.execute(text('SELECT id,role,group_id,is_active,password_hash FROM users')).mappings()
            conn.info['license_had_recovery'] = any(eligible_local(row) for row in rows)
        conn.info['license_membership_write'] = True
        context._license_membership_changed = True
    finally:
        conn.info['license_hook'] = False


def _after_membership(conn, cursor, statement, parameters, context, executemany):
    if not getattr(context, '_license_membership_changed', False):
        return
    conn.info['license_hook'] = True
    try:
        raw = conn.execute(text('SELECT value FROM system_config WHERE key=:k'), {'k': POLICY_KEY}).scalar()
        p = _decode(raw)
        if _valid_policy(p):
            p['revision'] = _next_revision(conn, p['revision'])
            p['membership_generation'] += 1
            # Force snapshot reconciliation even in this process.
            _cache.pop(str(conn.engine.url), None)
            conn.execute(text('UPDATE system_config SET value=:v WHERE key=:k'), {'k': POLICY_KEY, 'v': json.dumps(p)})
    finally:
        conn.info['license_hook'] = False


def _check_recovery_commit(conn):
    if not enabled() or not conn.info.get('license_membership_write'):
        _clear_membership(conn)
        return
    if conn.info.get('license_had_recovery'):
        rows = conn.execute(text('SELECT id,role,group_id,is_active,password_hash FROM users')).mappings()
        if not any(eligible_local(row) for row in rows):
            raise LicenseLockError('license_selection_invalid', status=422, reason='last_local_recovery_principal')
    _clear_membership(conn)


def _clear_membership(conn):
    # Runs on every rollback, including the one an invalidated connection fires
    # while it closes (the Postgres dispatch target lock does exactly that).
    # Reading .info there raises; an invalidated connection has nothing to clear.
    try:
        info = conn.info
    except Exception:
        return
    for key in ('license_had_recovery', 'license_membership_write', 'license_hook'):
        info.pop(key, None)


def _clear_membership_pool(dbapi_connection, connection_record):
    for key in ('license_had_recovery', 'license_membership_write', 'license_hook'):
        connection_record.info.pop(key, None)


install_membership_hooks()
