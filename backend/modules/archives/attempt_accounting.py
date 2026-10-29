"""Durable physical-attempt attribution, within the monitor's transaction.

Capture identities only. Captured slots do not prove which material was used or
how much; accounting must resolve an explicit complete mapping separately.
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from core.db_utils import _postgres_placeholders


def _json_value(value):
    """DBAPI JSON columns are decoded by PostgreSQL, but text on SQLite."""
    return json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value


def valid_ams_tray(value):
    """Bambu's 254/255 sentinels, booleans and fractional values are not trays."""
    if isinstance(value, str) and len(value) <= 2 and value.isascii() and value.isdecimal():
        value = int(value)
    return value if type(value) is int and 0 <= value < 16 else None


def _observed_trays_key(job_id, print_job_id):
    from core.filament_ledger import print_marker
    return print_marker(job_id, print_job_id) + ':observed_trays'


def record_attempt_tray(connection, print_job_id, printer_id, tray_now):
    """Union fresh telemetry into this running attempt; caller owns commit.

    Repeated/invalid values do no writes. Lock parents before the print row,
    just like terminal accounting, then reread to prevent lost PG updates.
    """
    tray = valid_ams_tray(tray_now)
    if tray is None:
        return False

    def read():
        row = execute(connection, "SELECT scheduled_job_id FROM print_jobs "
                      "WHERE id=? AND printer_id=? AND status='running' AND spool_snapshot IS NOT NULL",
                      (print_job_id, printer_id)).fetchone()
        if row is None:
            return None, []
        key = _observed_trays_key(row[0], print_job_id)
        stored = execute(connection, 'SELECT value FROM system_config WHERE key=?', (key,)).fetchone()
        return key, _json_value(stored[0]) if stored else []

    key, trays = read()
    if key is None or tray in trays:
        return False
    from core.order_production import prepare_print_job_write
    prepare_print_job_write(connection, print_job_id)
    key, trays = read()
    if key is None or tray in trays:
        return False
    execute(connection, 'INSERT INTO system_config(key,value) VALUES(?,?) '
            'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            (key, json.dumps(sorted([*trays, tray]))))
    return True


def mark_attempt_tray_evidence_incomplete(connection, print_job_id, printer_id):
    """Freeze missing evidence in the caller's locked terminal transaction.

    This marker survives monitor restarts and archive reconciliation. Never
    clear it after completion: later packets cannot repair an ended attempt.
    """
    from core.filament_ledger import print_marker
    row = execute(connection, 'SELECT scheduled_job_id FROM print_jobs WHERE id=? AND printer_id=?',
                  (print_job_id, printer_id)).fetchone()
    if row is not None:
        key = print_marker(row[0], print_job_id) + ':tray_evidence_incomplete'
        execute(connection, 'INSERT INTO system_config(key,value) VALUES(?,?) '
                'ON CONFLICT(key) DO NOTHING', (key, json.dumps(True)))


def resolve_attempt_material_plan(connection, snapshot, print_job_id):
    """Resolve only a frozen single-material Bambu plan awaiting tray evidence.

    Never reread file metadata or bindings. The ordinary accounting path still
    applies pending/confirmation, owner, balance and ledger checks.
    """
    plan = snapshot.get('material_plan')
    if (not isinstance(plan, dict) or plan.get('state') != 'unresolved'
            or plan.get('reason') != 'missing_material_mapping'
            or not isinstance(plan.get('observed_tray_fallback'), dict)):
        return plan
    from core.filament_ledger import print_marker
    incomplete = execute(connection, 'SELECT value FROM system_config WHERE key=?',
                         (print_marker(plan.get('scheduled_job_id'), print_job_id) + ':tray_evidence_incomplete',)).fetchone()
    if incomplete:
        return dict(plan, reason='tray_evidence_incomplete')
    stored = execute(connection, 'SELECT value FROM system_config WHERE key=?',
                     (_observed_trays_key(plan.get('scheduled_job_id'), print_job_id),)).fetchone()
    trays = _json_value(stored[0]) if stored else []
    if not trays:
        return plan
    if len(trays) != 1:
        return dict(plan, reason='multiple_trays_observed')
    tray = valid_ams_tray(trays[0])
    if tray is None:
        return plan
    matches = [slot for slot in snapshot.get('slots', []) if slot.get('slot_number') == tray + 1]
    if len(matches) > 1:
        return dict(plan, reason='ambiguous_start_spool')
    slot = matches[0] if matches else {'slot_number': tray + 1, 'spool_id': None, 'spool_org_id': None}
    if slot.get('spool_id') is not None and slot.get('spool_existed') is not True:
        return dict(plan, reason='missing_spool')
    if slot.get('spool_org_id') is not None and slot['spool_org_id'] != plan.get('org_id'):
        return dict(plan, reason='foreign_spool')
    allocation = {key: slot.get(key) for key in
                  ('slot_number', 'spool_id', 'spool_org_id', 'identity_state', 'observation_state')}
    allocation.update(plan['observed_tray_fallback'], source_slot=0)
    return dict(plan, state='complete', reason=None, allocations=[allocation])


def execute(connection, statement, parameters=()):
    """Use one bound SQL implementation for daemon DBAPI and SQLAlchemy callers."""
    if hasattr(connection, 'exec_driver_sql'):
        if connection.dialect.name == 'postgresql':
            statement = _postgres_placeholders(statement)
        return connection.exec_driver_sql(statement, tuple(parameters))
    return connection.execute(statement, tuple(parameters))


def _ledger_runner(connection):
    """core.filament_ledger ``run(sql, params)`` over this module's bound execute()."""
    from core.print_attempts import qmark_runner

    class _Bound:
        @staticmethod
        def execute(statement, parameters):
            return execute(connection, statement, parameters)
    return qmark_runner(_Bound())


def _finite_quantity(value):
    import math
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _purchase_inputs(price, net_weight):
    """Freeze raw purchase evidence, without guessing currency or valuation."""
    result = {'state': 'recorded', 'reason': None, 'price': None,
              'net_weight_g': None, 'weight_basis': 'stored_net_filament_weight',
              'currency': None, 'valuation_state': 'unknown_currency'}
    for key, value, positive in [('price', price, False), ('net_weight_g', net_weight, True)]:
        reason = None
        if value is None:
            reason = 'missing_price' if key == 'price' else 'missing_net_weight'
        else:
            try:
                if isinstance(value, bool):
                    raise ValueError('boolean purchase input')
                number = Decimal(str(value))
                if not number.is_finite() or number < 0 or (positive and number == 0):
                    raise ValueError('invalid purchase input')
                canonical = format(number, 'f')
                if '.' in canonical:
                    canonical = canonical.rstrip('0').rstrip('.')
                result[key] = '0' if number == 0 else canonical
            except (InvalidOperation, ValueError):
                reason = 'invalid_price' if key == 'price' else 'invalid_net_weight'
        if reason and result['reason'] is None:
            result.update(state='unresolved', reason=reason)
    return result


def _snapshot_slot(row):
    from core.tray_identity import decode
    state = decode(row[4])
    return {'slot_number': row[0], 'spool_id': row[1], 'spool_org_id': row[2],
            'spool_existed': row[3] is not None, 'observation_state': state.get('id'),
            'identity_state': state.get('hold_id', state.get('id')) if state.get('pending') else None}


def capture_attempt_spools(connection, print_job_id: int, printer_id: int, *, source: str, start_scope=None, start_plan=None) -> bool:
    """Capture only a newly created running attempt; never commit or overwrite.

    Callers must invoke this on first physical start, not when resuming an old
    running row. Missing historical attribution remains missing on recovery.
    """
    prospective = start_scope is not None or start_plan is not None
    material_fence = None
    if prospective:
        from core.economic_source_creation import SourceCreationError
        if start_scope is None or start_plan is None:
            raise SourceCreationError('Paired fresh capture scope and plan required')
        from .first_start_valuation import fence_material_membership
        material_fence = fence_material_membership(connection, start_scope, start_plan, print_job_id)
    locked = execute(connection,
        "UPDATE print_jobs SET id=id WHERE id=? AND printer_id=? "
        "AND status='running' AND spool_snapshot IS NULL",
        (print_job_id, printer_id))
    if locked.rowcount != 1:
        if prospective:
            raise SourceCreationError('Fresh capture row changed')
        return False
    printer = execute(connection, 'SELECT org_id FROM printers WHERE id=?', (printer_id,)).fetchone()
    if printer is None:
        if prospective:
            raise SourceCreationError('Fresh capture printer disappeared')
        return False
    slots = execute(connection,
        'SELECT fs.slot_number,fs.assigned_spool_id,s.org_id,s.id,fs.tray_observation '
        'FROM filament_slots fs LEFT JOIN spools s ON s.id=fs.assigned_spool_id '
        'WHERE fs.printer_id=? ORDER BY fs.slot_number,fs.id' + (' LIMIT 17' if prospective else ''), (printer_id,)).fetchall()
    located = execute(connection,
        "SELECT id,org_id FROM spools WHERE location_printer_id=? AND status='active' ORDER BY id" + (' LIMIT 17' if prospective else ''),
        (printer_id,)).fetchall()
    from .ownership import capture_content_owner
    scheduled = execute(connection, 'SELECT scheduled_job_id FROM print_jobs WHERE id=?', (print_job_id,)).fetchone()[0]
    snapshot = {
        'version': 1, 'printer_id': printer_id, 'printer_org_id': printer[0],
        'source': source,
        'content_owner': capture_content_owner(connection, scheduled),
        'material_plan': _material_plan(connection, print_job_id, printer_id, printer[0], slots, located, bounded=prospective),
        'slots': [_snapshot_slot(row) for row in slots],
        'located_spools': [{'spool_id': row[0], 'spool_org_id': row[1]} for row in located],
    }
    if prospective:
        from .first_start_valuation import freeze_unit_valuation
        snapshot['unit_valuation'] = freeze_unit_valuation(connection, start_scope, start_plan, print_job_id, snapshot['material_plan'], material_fence)
    changed = execute(connection,
        'UPDATE print_jobs SET spool_snapshot=? WHERE id=? AND printer_id=? AND spool_snapshot IS NULL',
        (json.dumps(snapshot, sort_keys=True, separators=(',', ':')), print_job_id, printer_id))
    if prospective:
        if changed.rowcount != 1:
            raise SourceCreationError('Fresh valuation snapshot changed')
        from core.attempt_start_plan import _complete_fresh_capture
        _complete_fresh_capture(connection, start_scope, start_plan, print_job_id)
    return changed.rowcount == 1


def _material_plan(connection, print_job_id, printer_id, printer_org_id, slots, located, *, bounded=False):
    """Freeze explicit file/mapping evidence, never distribute across guessed slots."""
    import math
    plan = {'state': 'unresolved', 'reason': 'missing_material_mapping',
            'scheduled_job_id': None, 'print_file_id': None,
            'org_id': printer_org_id, 'allocations': []}
    def unresolved(reason):
        plan.update(state='unresolved', reason=reason, allocations=[])
        return plan
    if bounded and (len(slots) > 16 or len(located) > 16):
        return unresolved('material_source_bound_exceeded')
    attempt = execute(connection, 'SELECT scheduled_job_id FROM print_jobs WHERE id=?', (print_job_id,)).fetchone()
    job_id = attempt[0] if attempt else None
    materials = None
    total = None
    mapping = None
    if job_id is not None:
        plan['scheduled_job_id'] = job_id
        job = execute(connection,
            'SELECT j.printer_id,j.charged_to_org_id,j.print_file_id,j.ams_mapping,'
            'pf.id,pf.org_id,pf.filament_weight_grams,pf.filaments_json '
            'FROM jobs j LEFT JOIN print_files pf ON pf.id=j.print_file_id WHERE j.id=?',
            (job_id,)).fetchone()
        if job is None:
            return unresolved('missing_scheduled_job')
        if job[0] is not None and job[0] != printer_id:
            return unresolved('foreign_job_printer')
        org = job[1] if job[1] is not None else printer_org_id
        plan['org_id'] = org
        if job[2] is not None:
            if job[4] is None:
                return unresolved('missing_explicit_file')
            file = job[4:8]
        else:
            files = execute(connection,
                'SELECT id,org_id,filament_weight_grams,filaments_json FROM print_files WHERE job_id=? ORDER BY id' + (' LIMIT 2' if bounded else ''),
                (job_id,)).fetchall()
            if len(files) != 1:
                return unresolved('ambiguous_legacy_file' if files else 'missing_file')
            file = files[0]
        plan['print_file_id'] = file[0]
        if file[1] is not None and file[1] != org:
            return unresolved('foreign_file')
        total = file[2]
        try:
            materials = json.loads(file[3]) if isinstance(file[3], str) else file[3]
            mapping = json.loads(job[3]) if isinstance(job[3], str) else job[3]
        except (ValueError, TypeError):
            return unresolved('invalid_material_metadata')
        if materials is not None and (not isinstance(materials, list) or len(materials) > 16
                                      or any(not isinstance(x, dict) for x in materials)):
            return unresolved('invalid_material_metadata')
    plan['estimated_total'] = total
    org = plan['org_id']
    assigned = [row for row in slots if row[1] is not None]
    identities = {row[1] for row in assigned}
    located_ids = {row[0] for row in located}
    if identities and located_ids and not located_ids <= identities:
        return unresolved('contradictory_start_identity')
    if mapping is not None:
        if not isinstance(mapping, list) or not mapping or any(type(x) is not int or not 0 <= x < 16 for x in mapping) or len(set(mapping)) != len(mapping):
            return unresolved('invalid_mapping')
        if not materials or len(mapping) != len(materials):
            return unresolved('incomplete_mapping')
        selected = []
        for target in mapping:
            matches = [row for row in slots if row[0] == target + 1]
            if len(matches) != 1:
                matches = [(target + 1, None, None, None, None)]
            selected.append(matches[0])
    else:
        if materials and len(materials) > 1:
            return unresolved('missing_material_mapping')
        printer = execute(connection, 'SELECT api_type,slot_count FROM printers WHERE id=?', (printer_id,)).fetchone()
        # A start-time AMS value may be cached from a different print.
        # Only fresh attempt observations can resolve an unmapped Bambu plan.
        target = None
        if printer and printer[0] != 'bambu' and printer[1] == 1:
            target = 1
        if target is None:
            if printer and printer[0] == 'bambu' and plan['print_file_id'] is not None:
                grams = materials[0].get('used_grams') if materials else None
                grams = total if grams is None else grams
                if any(value is not None and not _finite_quantity(value) for value in (grams, total)):
                    return unresolved('invalid_material_quantity')
                if grams is not None and total is not None and not math.isclose(grams, total, rel_tol=1e-6, abs_tol=0.1):
                    return unresolved('conflicting_material_quantities')
                plan['observed_tray_fallback'] = {
                    'estimated_grams': grams,
                    'quantity_provenance': 'sliced_estimate' if grams is not None else 'unknown'}
            return unresolved('missing_material_mapping')
        matches = [row for row in slots if row[0] == target]
        if len(matches) > 1:
            return unresolved('ambiguous_start_spool')
        selected = matches or [(target, None, None, None, None)]
    allocations = []
    for index, row in enumerate(selected):
        # Unbound trays are valid unattributed sources; a dangling assignment
        # must not become valid because a spool appears after the first read.
        if row[1] is not None and row[3] is None:
            return unresolved('missing_spool')
        if row[2] is not None and row[2] != org:
            return unresolved('foreign_spool')

        current = execute(connection, 'SELECT id,org_id,price,initial_weight_g FROM spools WHERE id=?', (row[1],)).fetchone()
        if row[1] is not None and current is None:
            return unresolved('missing_spool')
        if current is not None and (current[0] != row[1] or current[1] != row[2]):
            return unresolved('spool_identity_changed')
        grams = materials[index].get('used_grams') if materials else None
        if grams is None and len(selected) == 1:
            grams = total
        if grams is not None and (not _finite_quantity(grams)):
            return unresolved('invalid_material_quantity')
        from core.tray_identity import decode
        state = decode(row[4]) if len(row) > 4 else {}
        allocations.append({'observation_state': state.get('id'), 'identity_state': state.get('hold_id', state.get('id')) if state.get('pending') else None, 'spool_id': row[1], 'spool_org_id': row[2],
                            'source_slot': index, 'slot_number': row[0],
                            'estimated_grams': grams,
                            'purchase_inputs': _purchase_inputs(current[2], current[3]) if current else None,
                            'quantity_provenance': 'sliced_estimate' if grams is not None else 'unknown'})
    if total is not None:
        if not _finite_quantity(total):
            return unresolved('invalid_material_quantity')
        if all(x['estimated_grams'] is not None for x in allocations):
            summed = sum(x['estimated_grams'] for x in allocations)
            if not math.isfinite(summed) or not math.isclose(summed, total, rel_tol=1e-6, abs_tol=0.1):
                return unresolved('conflicting_material_quantities')
    plan.update(state='complete', reason=None, allocations=allocations)
    return plan


def account_archive(connection, *, archive_id, print_job_id, printer_id, snapshot_json,
                    scheduled_job_id, measured_grams, success):
    """Persist local consumption and its delivery outbox in the archive transaction.

    Return an explicit unresolved reason before any stock mutation when evidence
    cannot attribute the quantity. Caller owns the attempt lock and commit.
    """
    import math
    import uuid
    def unresolved(reason):
        return 'unresolved', reason, []
    if not success and measured_grams is None:
        return unresolved('non_successful_attempt_quantity_unconfirmed')
    try:
        snapshot = json.loads(snapshot_json) if isinstance(snapshot_json, str) else snapshot_json
        if not isinstance(snapshot, dict) or type(snapshot.get('version')) is not int or snapshot.get('version') != 1 or type(snapshot.get('printer_id')) is not int or snapshot.get('printer_id') != printer_id:
            return unresolved('missing_or_invalid_start_snapshot')
        plan = resolve_attempt_material_plan(connection, snapshot, print_job_id)
        if not isinstance(plan, dict) or plan.get('state') != 'complete':
            reason = plan.get('reason') if isinstance(plan, dict) else 'missing_material_plan'
            grams = measured_grams if measured_grams is not None else (plan or {}).get('estimated_total')
            execute(connection, 'UPDATE print_archives SET consumption_json=? WHERE id=?',
                    (json.dumps([dict(spool_id=None, grams_used=grams, reason=reason)]), archive_id))
            return unresolved(reason)
        if plan.get('scheduled_job_id') != scheduled_job_id:
            return unresolved('attempt_linkage_changed')
        allocations = plan.get('allocations')
        if not isinstance(allocations, list) or not allocations:
            return unresolved('missing_material_mapping')
        if measured_grams is not None and (not _finite_quantity(measured_grams)):
            return unresolved('invalid_measured_quantity')
        if not success and len(allocations) != 1:
            return unresolved('non_successful_material_distribution_unknown')
        allocations = [dict(item) for item in allocations]
        pending = []
        from core.tray_identity import decode
        held_row = execute(connection, 'SELECT consumption_json FROM print_archives WHERE id=?', (archive_id,)).fetchone()
        held = json.loads(held_row[0] or '[]') if held_row else []
        for item in allocations:
            identity = item.get('identity_state')
            retained = next((h.get('identity_state') for h in held if h.get('slot_number') == item.get('slot_number') and h.get('identity_state')), None)
            if retained:
                identity = retained
            if not identity:
                current = execute(connection, 'SELECT tray_observation FROM filament_slots WHERE printer_id=? AND slot_number=?',
                                  (printer_id, item.get('slot_number'))).fetchone()
                state = decode(current[0]) if current else {}
                if state.get('pending'):
                    identity = state.get('hold_id', state.get('id'))
                elif state.get('confirmation_id') and item.get('observation_state') and item['observation_state'] != state.get('id'):
                    identity = state['confirmation_id']
            item['identity_state'] = identity
            if identity:
                confirmed = execute(connection, 'SELECT c.spool_id,s.org_id FROM tray_confirmations c JOIN spools s ON s.id=c.spool_id '
                                    'JOIN filament_slots fs ON fs.id=c.slot_id WHERE c.state_id=? AND fs.printer_id=? AND fs.slot_number=?',
                                    (identity, printer_id, item.get('slot_number'))).fetchone()
                if confirmed:
                    item['captured_spool_id'] = item['spool_id']
                    item['captured_spool_org_id'] = item.get('spool_org_id')
                    item['spool_id'], item['spool_org_id'] = confirmed
                else:
                    pending.append(identity)
            elif item.get('spool_id') is None:
                pending.append(None)
        if pending:
            entries = [dict(spool_id=None, slot_number=item.get('slot_number'), identity_state=item.get('identity_state'),
                            grams_used=measured_grams if len(allocations) == 1 and measured_grams is not None else item.get('estimated_grams'),
                            reason='spool_change_pending' if item.get('identity_state') else 'no_bound_spool') for item in allocations]
            execute(connection, 'UPDATE print_archives SET consumption_json=? WHERE id=?', (json.dumps(entries), archive_id))
            return 'unresolved', 'spool_change_pending' if any(pending) else 'no_bound_spool', entries
        consumption = {}
        for item in allocations:
            if not isinstance(item, dict) or type(item.get('spool_id')) is not int or item['spool_id'] <= 0:
                return unresolved('invalid_captured_spool')
            slot = item.get('slot_number')
            if slot is None:
                original = [row for row in snapshot.get('located_spools', [])
                            if isinstance(row, dict) and row.get('spool_id') == item['spool_id']]
            else:
                original = [row for row in snapshot.get('slots', [])
                            if isinstance(row, dict) and row.get('slot_number') == slot]
                if len(original) != 1 or (original[0].get('spool_existed') is not True and not ('captured_spool_id' in item and item['captured_spool_id'] is None)):
                    return unresolved('contradictory_start_identity')
            if len(original) != 1 or original[0].get('spool_id') != item.get('captured_spool_id', item['spool_id']) or original[0].get('spool_org_id') != item.get('captured_spool_org_id', item.get('spool_org_id')):
                return unresolved('contradictory_start_identity')
            grams = item.get('estimated_grams')
            provenance = 'sliced_estimate'
            if len(allocations) == 1 and measured_grams is not None:
                grams, provenance = measured_grams, 'measured'
            if grams is None:
                return unresolved('unknown_material_quantity')
            if not _finite_quantity(grams):
                return unresolved('invalid_material_quantity')
            spool_id = item['spool_id']
            prior = consumption.get(spool_id)
            if prior is not None and prior['spool_org_id'] != item.get('spool_org_id'):
                return unresolved('contradictory_start_identity')
            if prior is None:
                consumption[spool_id] = {'spool_id': spool_id, 'spool_org_id': item.get('spool_org_id'),
                                         'grams_used': grams, 'quantity_provenance': provenance}
            else:
                prior['grams_used'] += grams
                if not math.isfinite(prior['grams_used']):
                    return unresolved('invalid_material_quantity')
        total = sum(item['grams_used'] for item in consumption.values())
        if not math.isfinite(total):
            return unresolved('invalid_material_quantity')
        if len(allocations) > 1 and measured_grams is not None and not math.isclose(total, measured_grams, rel_tol=1e-6, abs_tol=0.1):
            return unresolved('measured_material_distribution_unknown')
    except (ValueError, TypeError, KeyError):
        return unresolved('invalid_start_snapshot')
    # One ledger for every deducer (core.filament_ledger): this physical print
    # may already be accounted (by the Moonraker monitor), or its attempt may hold
    # a job-level deduction (operator /complete) or a marker-less row that
    # cannot be attributed to this print -- reconcile instead of deducting twice.
    from core import filament_ledger
    ledger_run = _ledger_runner(connection)
    prior = filament_ledger.prior_attempt_usage(ledger_run, scheduled_job_id, print_job_id)
    if prior == filament_ledger.MARKER:
        return 'accounted', 'attempt_already_accounted', []
    if prior == filament_ledger.PRINT_USAGE:
        return unresolved('attempt_usage_without_archive')
    if prior == filament_ledger.LEGACY_JOB_USAGE:
        return unresolved('legacy_usage_unattributed')
    if prior == filament_ledger.JOB_USAGE:
        return unresolved('job_usage_unattributed')
    rows = {}
    confirmed_ids = {item["spool_id"] for item in allocations if "captured_spool_id" in item}
    for spool_id in sorted(consumption):
        # Portable SQLite writer / PostgreSQL row lock, then read fresh owner
        # and balance while holding every spool lock until caller commits.
        locked = execute(connection, 'UPDATE spools SET id=id WHERE id=?', (spool_id,))
        if locked.rowcount != 1:
            return unresolved('missing_spool')
        row = execute(connection,
            'SELECT org_id,remaining_weight_g,spoolman_spool_id FROM spools WHERE id=?',
            (spool_id,)).fetchone()
        item = consumption[spool_id]
        if row[0] != item['spool_org_id'] or (row[0] is not None and row[0] != plan.get('org_id')):
            return unresolved('spool_owner_changed')
        if (row[1] is None and spool_id not in confirmed_ids) or (row[1] is not None and (type(row[1]) not in (int, float) or not math.isfinite(row[1]) or row[1] < 0)):
            return unresolved('unknown_spool_balance')
        rows[spool_id] = row
    outcome = filament_ledger.record_job_filament(
        ledger_run, scheduled_job_id, source='archive', print_job_id=print_job_id,
        grams_by_spool={spool_id: consumption[spool_id]['grams_used'] for spool_id in consumption},
        allow_unknown_balance=bool(confirmed_ids))
    if outcome.state == 'already_deducted':
        if outcome.reason == filament_ledger.JOB_USAGE:
            return unresolved('job_usage_unattributed')
        return 'accounted', 'attempt_already_accounted', []
    zero = [spool_id for spool_id in sorted(consumption) if consumption[spool_id]['grams_used'] == 0]
    if not outcome.deducted and len(zero) != len(consumption):
        return unresolved('nothing_deducted')
    result = [{'spool_id': spool_id, 'grams_used': consumption[spool_id]['grams_used'],
               'quantity_provenance': consumption[spool_id]['quantity_provenance'],
               'inventory_shortfall_g': 0} for spool_id in zero]
    has_shortfall = False
    for entry in outcome.deductions:
        spool_id = entry['spool_id']
        grams = consumption[spool_id]['grams_used']  # as attributed (ledger stores it as float)
        if entry['spoolman_spool_id'] is not None and grams > 0:
            execute(connection,
                "INSERT INTO filament_delivery_outbox(id,print_job_id,spool_id,spoolman_spool_id,grams_used,state) VALUES(?,?,?,?,?,'pending')",
                (uuid.uuid4().hex, print_job_id, spool_id, entry['spoolman_spool_id'], grams))
        has_shortfall = has_shortfall or entry['shortfall_g'] > 0
        result.append({'spool_id': spool_id, 'grams_used': grams,
                       'quantity_provenance': consumption[spool_id]['quantity_provenance'],
                       'inventory_shortfall_g': entry['shortfall_g'], 'remaining_unknown': entry['remaining_g'] is None})
    result.sort(key=lambda item: item['spool_id'])
    execute(connection, 'UPDATE print_archives SET consumption_json=? WHERE id=?',
            (json.dumps(result, allow_nan=False), archive_id))
    return ('reconciliation_required', 'inventory_shortfall', result) if has_shortfall else ('accounted', None, result)
