"""Standalone 027 command admission. No restart adoption, TTL or retry.

Configured-target exclusion covers participating ODIN processes, not external
controllers, DNS aliases or arbitrary configuration writers. Stable mappings only:
configuration writers do not yet all acquire old/new target locks. HTTP results
report relay state, not electrical measurement. HA readback/MQTT PUBACK cannot
prove queued-command quiescence and are refused before publication.
"""
from datetime import datetime, timezone
import hmac
import json
import time
from urllib.parse import urlsplit
from sqlalchemy import text
from core.dispatch_identity import build_identity, ownership_refusal, _host
from core.dispatch_target_lock import acquire_target_lock
from core.dispatch_target_guard import unresolved_target
from core.power_target_operations import PowerFacts, _stage_operation, _stage_transition

_uncertain_handles = []


def _key():
    from core.auth import SECRET_KEY
    return SECRET_KEY.encode() if isinstance(SECRET_KEY, str) else SECRET_KEY


def _digest(value):
    return hmac.digest(_key(), b'ODIN power fence v1\0' + json.dumps(
        value, sort_keys=True, default=str, separators=(',', ':')).encode(), 'sha256').hex()


def _destination(config):
    # Match the actual direct Tasmota URL builder; reject path/query/userinfo
    # injection and ambiguous endpoints rather than invent alias equivalence.
    if config['type'] != 'tasmota':
        return None
    host = config['host']
    if (not isinstance(host, str) or host != host.strip()
            or any(char.isspace() or ord(char) < 33 for char in host)
            or any(char in host for char in ('?', '#', '@')) or host.endswith(':')):
        return None
    parsed = urlsplit('http://' + host)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.path
            or parsed.query or parsed.fragment or parsed.port == 0):
        return None
    return _digest(['tasmota', 'http', _host(parsed.hostname), parsed.port or 80, 'Power'])


def _utc(value):
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)


def _snapshot(module, printer_id, purpose, physical_id=None, *, admission=True):
    from modules.printers.dispatch import _get_printer_info, _load_job, _authorize_education_hardware_action
    creds = _get_printer_info(printer_id)
    identity = build_identity(creds, _key())
    config = module.get_plug_config(printer_id, include_storage_identity=True)
    if identity is None or not creds['is_active'] or not config:
        return None
    destination = _destination(config)
    if destination is None:
        return None
    config.pop('power_state', None)
    automatic = purpose in ('observed_on', 'cooldown_off')
    if automatic and not config['auto_on' if purpose == 'observed_on' else 'auto_off']:
        return None
    with module.engine.connect() as conn:
        # Predicate discovery rejects duplicate configured targets and shared
        # destinations. This is not a fence against nonparticipating writers.
        rows = conn.execute(text('SELECT id FROM printers WHERE plug_type IS NOT NULL')).scalars().all()
        for other in rows:
            if other == printer_id:
                continue
            other_config = module.get_plug_config(other, include_storage_identity=True)
            other_identity = build_identity(_get_printer_info(other), _key())
            if (other_config and _destination(other_config) == destination
                    or other_identity and other_identity.target_key == identity.target_key):
                return None
        observation = None
        scheduled_id = None
        if automatic:
            if type(physical_id) is not int or physical_id <= 0:
                return None
            row = conn.execute(text('SELECT * FROM print_jobs WHERE id=:id AND printer_id=:pid'),
                               {'id': physical_id, 'pid': printer_id}).mappings().first()
            if row is None or row['observed_received_at'] is None:
                return None
            observation = dict(row)
            # Education/reserved observations never become generic permits.
            if any('odin-' in str(row.get(n) or '').lower()
                    for n in ('filename', 'job_name')):
                return None
            scheduled_id = row['scheduled_job_id']
            if scheduled_id is not None:
                job = _load_job(scheduled_id)
                expected_status = 'printing' if purpose == 'observed_on' else 'completed'
                if (not job or job['printer_id'] != printer_id or job['status'] != expected_status
                        or job.get('education_submission_id') is not None
                        or ownership_refusal(job, creds)
                        or _authorize_education_hardware_action(job, creds) is not None):
                    return None
                observation = {'physical': observation, 'scheduled': job}
            if conn.execute(text("SELECT 1 FROM education_monitor_claims WHERE printer_id=:pid AND state='running'"), {'pid': printer_id}).first():
                return None
            if admission:
                telemetry = conn.execute(text('SELECT gcode_state,last_seen FROM printers WHERE id=:pid'), {'pid': printer_id}).first()
                now = _utc(conn.execute(text('SELECT clock_timestamp()' if conn.dialect.name == 'postgresql' else "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')")).scalar_one())
                if not telemetry or not telemetry[1] or not 0 <= (now - _utc(telemetry[1])).total_seconds() <= 90:
                    return None
                if purpose == 'observed_on':
                    if conn.execute(text("SELECT 1 FROM print_jobs WHERE printer_id=:pid AND status='running' AND id<>:id LIMIT 1"), {'pid': printer_id, 'id': physical_id}).first():
                        return None
                    if (row['status'] != 'running' or telemetry[0] not in ('RUNNING', 'PRINTING')
                            or not 0 <= (now - _utc(row['observed_received_at'])).total_seconds() <= 90):
                        return None
                elif row['status'] not in ('completed', 'finished', 'success') or not module._auto_off_safe(printer_id):
                    return None
    # Cache/telemetry changes are not configuration changes. Full observation
    # identity is captured separately and must remain exact at each boundary.
    mapping = {k: v for k, v in config.items() if k != 'power_state'}
    facts = PowerFacts(printer_id=printer_id, target_key=identity.target_key,
                       config_key=_digest([identity.configuration_key, mapping]),
                       destination_key=destination, purpose=purpose,
                       physical_job_id=physical_id if automatic else None,
                       scheduled_job_id=scheduled_id,
                       observation_key=_digest(observation) if automatic else None)
    return facts, config


def _confirm(engine, owner, facts, state, revision):
    with engine.connect() as conn:
        names = tuple(PowerFacts.__dataclass_fields__)
        row = conn.execute(text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe: internal constant SQL fragments only; all values bound
            'SELECT ' + ','.join(names) + ',state,revision,owner_nonce FROM power_target_operations WHERE operation_nonce=:n'),
                           {'n': owner.operation_nonce}).first()
        return row is not None and tuple(row) == (*(getattr(facts, n) for n in names), state, revision, owner.owner_nonce)


def run(module, printer_id, purpose, *, physical_id=None, expected=None, timer=None, authority=None):
    """One owned command. Every ambiguous commit leaves a durable blocking row.

    30s covers target/local acquisition, worker/result admission; DB/pool and OS
    waits keep their configured bounds. Cleanup may add two fixed one-second joins.
    """
    deadline = time.monotonic() + 30
    handle = lock = owner = None
    acquired = False
    state, revision = 'preparing', 0
    result = None
    release_ready = False
    retain_handle = False
    try:
        if authority is not None:
            from modules.printers.power_authority import ManualPowerAuthority
            if type(authority) is not ManualPowerAuthority:
                return None
        initial = _snapshot(module, printer_id, purpose, physical_id)
        if initial is None:
            return None
        facts, config = initial
        if authority is not None:
            facts = authority.facts(facts)
        if expected is not None and initial != expected:
            return None
        target = acquire_target_lock(module.engine, facts.target_key)
        if target.status != 'acquired':
            return None
        handle = target.handle
        if not handle.held():
            return None
        lock = module._printer_power_lock(printer_id)
        acquired = lock.acquire(timeout=max(0, deadline - time.monotonic()))
        if not acquired:
            return None
        def fresh():
            return (time.monotonic() < deadline and handle.held()
                    and (timer is None or module._cooldown_timers.get(printer_id) is timer)
                    and _snapshot(module, printer_id, purpose, physical_id) == initial
                    and (authority is None or authority.valid())
                    and not unresolved_target(module.engine, printer_id, facts.target_key,
                                              include_claims=True, power_owner=owner))
        if not fresh():
            return None
        if authority is None:
            # A repeated producer event is not a fresh command authorization,
            # even after known success. Completed history never grants replay.
            with module.engine.connect() as conn:
                if conn.execute(text('SELECT 1 FROM power_target_operations WHERE printer_id=:pid AND purpose=:purpose AND physical_job_id=:physical LIMIT 1'),
                                {'pid': printer_id, 'purpose': purpose, 'physical': physical_id}).first():
                    return None
        if authority is not None:
            from core.power_target_operations import _lookup_replay
            with module.engine.begin() as conn:
                replay = _lookup_replay(conn, facts)
            if replay is not None:
                return replay == 'on_observed'
        def admit_in(conn):
            # Revalidate under the SAME short transaction that stages the CAS.
            # The guard obtains the 027 predicate boundary (SQLite writer/PG
            # table fence); selected credential locks stay until caller commit.
            if unresolved_target(module.engine, printer_id, facts.target_key,
                    include_claims=True, power_owner=owner, connection=conn):
                return False
            return (handle.held() and time.monotonic() < deadline
                    and (timer is None or module._cooldown_timers.get(printer_id) is timer)
                    and _snapshot(module, printer_id, purpose, physical_id) == initial
                    and (authority is None or authority.valid(conn)))

        # No session or row lock survives these short transactions into I/O.
        with module.engine.begin() as conn:
            if conn.dialect.name == 'postgresql':
                conn.execute(text("SET LOCAL lock_timeout='2000ms'"))
                conn.execute(text('LOCK TABLE power_target_operations,printers IN SHARE ROW EXCLUSIVE MODE NOWAIT'))
            if not admit_in(conn):
                return None
            owner = _stage_operation(conn, facts)
        if not _confirm(module.engine, owner, facts, state, revision) or not fresh():
            return None
        with module.engine.begin() as conn:
            if not admit_in(conn):
                return None
            _stage_transition(conn, owner, revision, state, 'command_started')
        state, revision = 'command_started', 1
        if not _confirm(module.engine, owner, facts, state, revision) or not fresh():
            return None
        from modules.printers.plug_power_acquisition import bounded_http_power_action, _OwnedHttpResult
        action = 'TOGGLE' if purpose == 'manual_toggle' else 'OFF' if purpose in ('cooldown_off', 'manual_off') else 'ON'
        receipt = bounded_http_power_action(config, deadline, action)
        observed = receipt.attempt.power_state if type(receipt) is _OwnedHttpResult else None
        expected_state = observed if action == 'TOGGLE' else action == 'ON'
        retain_handle = type(receipt) is _OwnedHttpResult and not receipt.attempt.reaped
        if type(receipt) is not _OwnedHttpResult or not receipt.confirmed(expected_state) or not fresh():
            return None
        if not module._conditional_power_observation(printer_id, observed, config,
                enable_flag='auto_on' if purpose == 'observed_on' else 'auto_off' if purpose == 'cooldown_off' else None):
            return None
        with module.engine.begin() as conn:
            _stage_transition(conn, owner, revision, state, 'release_pending',
                              outcome='on_observed' if observed else 'off_observed')
        state, revision = 'release_pending', 2
        release_ready = True
        result = observed
    except Exception:
        # Never log raw configuration, credentials, DB exception parameters or IPC.
        module.log.warning('Power fence command was refused or remains unresolved')
    finally:
        if acquired:
            if timer is not None and module._cooldown_timers.get(printer_id) is timer:
                del module._cooldown_timers[printer_id]
            lock.release()
        released = False
        if handle is not None and retain_handle:
            _uncertain_handles.append(handle)
        elif handle is not None:
            try:
                released = handle.close().released is True
            except Exception:
                pass
            if not released:
                _uncertain_handles.append(handle)
        if owner is not None:
            try:
                with module.engine.begin() as conn:
                    if release_ready and released:
                        _stage_transition(conn, owner, revision, state, 'complete')
                    else:
                        _stage_transition(conn, owner, revision, state, 'cleanup_uncertain',
                                          reason='command_uncertain')
                if not (release_ready and released):
                    result = None
            except Exception:
                # Ambiguous CAS/commit is NEVER retried or auto-cleared, even
                # when the remote result was normal. Existing row stays blocking.
                result = None
    return result
