"""Install-wide license policy. Dormant until ODIN_LICENSE_FLEET_LOCK_ENABLED.

B1 owns evidence, selections and user authorization. Physical-attempt capture and
hardware admission are B2; no hardware permission is inferred here.
"""
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
import os
import re
from types import MappingProxyType
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import text

POLICY_KEY = 'license_active_selection'
EVIDENCE_KEY = 'license_lock_evidence'
REVISION_KEY = 'license_lock_revision'


def enabled():
    return os.getenv('ODIN_LICENSE_FLEET_LOCK_ENABLED', '').lower() in {'1', 'true', 'yes', 'on'}


def paid_lapse_enabled():
    return os.getenv('ODIN_LICENSE_PAID_LAPSE_ENABLED', '').lower() in {'1', 'true', 'yes', 'on'}


# Deliberately no restricted-session issuer in B1. An environment flag alone
# cannot safely enable a scope whose routes and operator decision are pending.
RESTRICTED_RECOVERY_ENABLED = False
COMMUNITY_DOWNGRADE_ENABLED = False


class LicenseLockError(HTTPException):
    def __init__(self, code, *, status=403, revision=0, resource_id=None, reason=None):
        detail = dict(code=code, message=code.replace('_', ' '), resource_type='user',
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
    return (set(e) <= {'state', 'digest', 'binding_digest', 'max_printers', 'max_users', 'enforce', 'reason', 'deadline', 'lease_deadline'}
            and e.get('state') in {'verified_current', 'verified_expired', 'community_initial', 'community_selected', 'verification_unavailable'}
            and type(e.get('enforce')) is bool
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
                deadline=info.expiry_deadline.isoformat() if info.expiry_deadline else None)


def _evidence(db, info, previous):
    """Persist only verified envelopes; reverify saved evidence before leasing it."""
    from license_manager import verify_license_text, license_clock
    from core.license_clock import instant
    now, uncertain = license_clock().sample()
    try:
        evidence = _read(db, EVIDENCE_KEY)
    except (ValueError, TypeError):
        evidence = None
    if info.verification_state in {'verified_current', 'verified_expired'}:
        new = dict(schema_version=1, signed_envelope=info.signed_envelope, sha256=info.verified_digest,
                   binding_digest=info.binding_digest, accepted_at=now.isoformat(), time_anchor=now.isoformat(),
                   verification_state=info.verification_state,
                   uncertainty_started_at=None, uncertainty_deadline=None)
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
    if info.verification_state == 'community_initial' and not evidence:
        return _entitlement(info), None
    warning = 'license_verification_unavailable'
    unavailable = dict(_entitlement(info), state='verification_unavailable', enforce=False)
    if previous:
        unavailable.update(max_printers=previous.get('max_printers'), max_users=previous.get('max_users'))
    if not isinstance(evidence, dict) or evidence.get('schema_version') != 1:
        return unavailable, warning
    if not evidence.get('uncertainty_started_at'):
        evidence['uncertainty_started_at'] = now.isoformat()
        evidence['uncertainty_deadline'] = (now + timedelta(hours=24)).isoformat()
        _write(db, EVIDENCE_KEY, evidence)
    raw = evidence.get('signed_envelope')
    if not isinstance(raw, str) or hashlib.sha256(raw.encode()).hexdigest() != evidence.get('sha256'):
        return unavailable, warning
    verified = verify_license_text(raw)
    # Clock uncertainty cannot support elapsed lease time; signature/schema and
    # current binding must still match. Never trust a cached resolved cap alone.
    usable = (verified.verified_digest == evidence.get('sha256') and
              verified.binding_digest == evidence.get('binding_digest'))
    if not usable:
        return unavailable, warning
    if not evidence.get('uncertainty_started_at'):
        evidence['uncertainty_started_at'] = now.isoformat()
        evidence['uncertainty_deadline'] = (now + timedelta(hours=24)).isoformat()
    try:
        anchor = instant(evidence['time_anchor'])
        deadline = instant(evidence['uncertainty_deadline'])
        elapsed_trusted = (license_clock().anchor is not None and
                           getattr(license_clock(), 'reason_code', None) != 'invalid_clock_arithmetic')
        if (uncertain and not elapsed_trusted) or now < anchor or now - anchor > timedelta(hours=24) and not previous:
            deadline = min(deadline, now)
            evidence['uncertainty_deadline'] = deadline.isoformat()
    except (ValueError, TypeError, KeyError):
        evidence['uncertainty_deadline'] = now.isoformat()
        deadline = now
    _write(db, EVIDENCE_KEY, evidence)
    if now < deadline and previous and previous.get('digest') == verified.verified_digest:
        try:
            accepted = instant(evidence['accepted_at'])
            proven_caps = ((verified.signed_max_printers, verified.signed_max_users)
                           if accepted < verified.expiry_deadline else (5, 1))
        except (ValueError, TypeError, KeyError):
            return unavailable, warning
        if (previous.get('max_printers'), previous.get('max_users')) != proven_caps:
            return unavailable, 'license_policy_evidence_mismatch'
        frozen = dict(previous, state='verification_unavailable', reason='uncertainty_lease', lease_deadline=deadline.isoformat())
        return frozen, warning
    if previous:
        unavailable.update(max_printers=previous.get('max_printers'), max_users=previous.get('max_users'))
    return unavailable, warning


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
    picks = selection or {'printers': old['printers'], 'users': old['users']}
    if selection is not None:
        for name, active in [('printers', printers), ('users', users)]:
            ids, cap = picks.get(name), entitlement.get('max_' + name)
            if not _valid_ids(ids) or any(i not in active for i in ids) or (cap is not None and len(ids) > cap):
                raise LicenseLockError('license_selection_invalid', status=422, revision=revision)
        if entitlement.get('max_users') != 0 and recovery is not None and picks['users'] and recovery not in picks['users']:
            raise LicenseLockError('license_selection_invalid', status=422, revision=revision, reason='recovery_seat_required')
    selection_caps = old['entitlement'] if entitlement['state'] == 'verification_unavailable' and old['entitlement'] else entitlement
    resolved = dict(old, entitlement=entitlement, recovery_user_id=recovery,
                    printers=_fill(picks['printers'], printers, selection_caps.get('max_printers')),
                    users=(_fill(picks['users'], users, selection_caps.get('max_users'), recovery) if recovery is not None or selection_caps.get('max_users') is None or not entitlement.get('enforce') else []),
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
        return self.entitlement.get('lease_deadline') or self.entitlement.get('deadline')

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
        lease = p.get('entitlement', {}).get('lease_deadline') if _valid_policy(p) else None
        from core.license_clock import instant
        fresh = (cached and _valid_policy(p) and cached[0] == p['revision'] and cached[1] == signature
                 and (not lease or now < instant(lease)))
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
            _cache[cache_key] = (p['revision'], signature, result)
        if holder is not None:
            holder.license_policy = result
        return result
    except HTTPException:
        raise
    except Exception:
        raise LicenseLockError('license_policy_unavailable', status=503) from None


def user_allowed(policy, user_id):
    return not policy.enforcing or user_id in policy.users


def require_user(policy, user_id, purpose='access'):
    if purpose in {'logout', 'credential_repair', 'revoke', 'enrollment_cancel'}:
        return
    if not user_allowed(policy, user_id):
        raise LicenseLockError('license_user_locked', revision=policy.revision, resource_id=user_id,
                               reason=policy.entitlement.get('reason'))


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
    for key in ('license_had_recovery', 'license_membership_write', 'license_hook'):
        conn.info.pop(key, None)


def _clear_membership_pool(dbapi_connection, connection_record):
    for key in ('license_had_recovery', 'license_membership_write', 'license_hook'):
        connection_record.info.pop(key, None)


install_membership_hooks()
