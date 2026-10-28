"""Caller-owned, organization-bound AMS inventory updates for raw daemon DBs."""
import json
from core.filament_ledger import record_ams_measurement
from core.print_attempts import qmark_runner
import uuid
from core.interfaces.rfid_identity import rfid_identity

from core.base import slot_filament_value
from core.material_identity import canonical_reported_material
from modules.printers.telemetry.validators import coerce_int_or_raise


class AMSInventoryError(ValueError):
    """Safe local association identifiers only; never raw telemetry or credentials."""
    def __init__(self, reason, printer_id, *, slots=(), spools=(), numbers=()):
        super().__init__(f"AMS association {reason}: printer_id={printer_id} "
                         f"filament_slot_ids={sorted(slots)} spool_ids={sorted(spools)} slot_numbers={sorted(numbers)}")


def remaining_percent(value):
    if value is None:
        return None
    try:
        result = coerce_int_or_raise(value)
    except ValueError:
        return None
    return result if 0 <= result <= 100 else None


_SPOOL_COLUMNS = 'id,org_id,location_printer_id,initial_weight_g,remaining_weight_g,location_slot'
# Closed set of table -> column lists that _rows may select.
_ROW_SELECTIONS = {'spools': frozenset({_SPOOL_COLUMNS}), 'printers': frozenset({'id', 'id,org_id'}),
                   'filament_slots': frozenset({'id'})}


def _rows(conn, table, ids, columns, lock=False):
    if columns not in _ROW_SELECTIONS.get(table, ()):
        raise ValueError('Unsupported AMS inventory selection')
    if not ids:
        return []
    placeholders = ','.join('?' for _ in ids)
    suffix = ' FOR UPDATE NOWAIT' if lock and getattr(conn, '_postgres', False) else ''
    return conn.execute(f'SELECT {columns} FROM {table} WHERE id IN ({placeholders}) ORDER BY id{suffix}', tuple(sorted(ids))).fetchall()  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- table/columns checked against the _ROW_SELECTIONS allowlist; placeholders are generated ?; values are bound


def _graph(conn, printer_id, tags, uuids=(), trays=()):
    rfid = rfid_identity()
    slots = conn.execute('SELECT id,assigned_spool_id,printer_id,slot_number FROM filament_slots WHERE printer_id=? ORDER BY id', (printer_id,)).fetchall()
    matches = []
    if tags:
        placeholders = ','.join('?' for _ in tags)
        matches = conn.execute(f'SELECT id,rfid_tag,tray_uuid FROM spools WHERE rfid_tag IN ({placeholders})', tuple(tags)).fetchall()  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- only '?' placeholders generated from the bound value count; values are bound
    for tray_uuid in uuids:
        matches.extend(conn.execute('SELECT id,rfid_tag,tray_uuid FROM spools WHERE tray_uuid=?', (tray_uuid,)).fetchall())
    matches = list({row[0]: row for row in matches}.values())
    org_row = conn.execute('SELECT org_id FROM printers WHERE id=?', (printer_id,)).fetchone()
    candidates = rfid.raw_candidate_rows(conn, org_row[0]) if trays and org_row else []
    ids = {row[1] for row in slots if row[1] is not None} | {row[0] for row in matches}
    ids.update(row[0] for row in conn.execute('SELECT id FROM spools WHERE location_printer_id=?', (printer_id,)).fetchall())
    ids.update(row[0] for row in candidates)
    spools = _rows(conn, 'spools', ids, _SPOOL_COLUMNS)
    if ids:
        placeholders = ','.join('?' for _ in ids)
        previous = conn.execute(f'SELECT id,assigned_spool_id,printer_id,slot_number FROM filament_slots WHERE assigned_spool_id IN ({placeholders})', tuple(sorted(ids))).fetchall()  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- only '?' placeholders generated from the bound value count; values are bound
        slots = list({row[0]: row for row in slots + previous}.values())
    repairs = {}
    for spool in spools:
        if spool[2] is not None:
            located = conn.execute('SELECT id,assigned_spool_id,printer_id,slot_number FROM filament_slots WHERE printer_id=? AND slot_number=?', (spool[2], spool[5])).fetchone()
            if located is None or located[1] != spool[0]:
                linked = [row for row in slots if row[1] == spool[0]]
                if linked:
                    raise AMSInventoryError('contradictory', printer_id, slots=[r[0] for r in linked], spools=(spool[0],))
                repairs[spool[0]] = (spool[2], spool[5])
                continue
            slots = list({row[0]: row for row in slots + [located]}.values())
    if {row[0] for row in spools} != ids:
        raise AMSInventoryError('unavailable', printer_id, slots=[row[0] for row in slots], spools=ids)
    for spool_id in ids:
        linked = [r for r in slots if r[1] == spool_id]
        if len(linked) > 1:
            raise AMSInventoryError('ambiguous', printer_id, slots=[r[0] for r in linked], spools=(spool_id,))
        if any(r[2] != printer_id for r in linked) and spool_id not in {r[0] for r in matches}:
            raise AMSInventoryError('cross-printer', printer_id, slots=[r[0] for r in linked], spools=(spool_id,))
    return ({row[0]: (row[1],row[2],row[3]) for row in slots},
            {row[0]: (None, None) if row[0] in repairs else (row[2],row[5]) for row in spools},
            {**{row[1]: row[0] for row in matches if row[1]},
             **{'uuid:' + row[2]: row[0] for row in matches if row[2]}}, repairs, candidates)


def sync_ams_inventory(conn, printer_id, units):
    """No commit: caller rolls back failure and advances throttle after commit.

    Existing rows are locked and rechecked; late absent RFID matches are never
    adopted. Arbitrary writers inserting new associations remain a separate
    concurrency acceptance requirement.
    """
    rfid = rfid_identity()
    from modules.printers.telemetry.bambu.ams_identity import ams_trays_by_slot, is_rfid_tag
    identified = ams_trays_by_slot(units, strict=True)
    identified = [(number, {**tray, 'tag_uid': rfid.identity(tray.get('tag_uid')),
                            'tray_uuid': rfid.identity(tray.get('tray_uuid'))}) for number, tray in identified]
    def material_reported(tray):
        value = tray.get('tray_type')
        return value == '' or canonical_reported_material(value) is not None

    material_incomplete = any(not material_reported(tray) for _, tray in identified)
    reported_slots = {number for number, _ in identified}
    had_trays = bool(identified)
    # Unknown material is a delta, not evidence of an empty tray. Skipping the
    # whole tray also preserves association and loaded_at freshness.
    identified = [(number, tray) for number, tray in identified
                  if material_reported(tray)]
    trays = [tray for _, tray in identified]
    if not trays and had_trays:
        return 0
    if not trays:
        # An empty AMS list still means every HT unit is unplugged.
        if not getattr(conn, '_postgres', False):
            conn.execute('UPDATE printers SET name=name WHERE id=?', (printer_id,))
        else:
            _rows(conn, 'printers', {printer_id}, 'id', lock=True)
        _clear_unplugged_ht(conn, printer_id, set())
        return 0
    reported_tags = [tray.get('tag_uid') for tray in trays if tray.get('tag_uid') and tray.get('tray_type')]
    if len(reported_tags) != len(set(reported_tags)):
        raise AMSInventoryError('ambiguous', printer_id, numbers=[number for number, _ in identified])
    tags = sorted(set(reported_tags))
    uuids = [tray['tray_uuid'] for tray in trays if tray['tray_uuid'] and tray.get('tray_type')]
    if len(uuids) != len(set(uuids)):
        raise AMSInventoryError('ambiguous', printer_id, numbers=[number for number, _ in identified])
    if not getattr(conn, '_postgres', False):
        conn.execute('UPDATE printers SET name=name WHERE id=?', (printer_id,))
    before = _graph(conn, printer_id, tags, uuids, trays)
    printer_ids = {v[0] for v in before[3].values() if v[0] is not None} | {printer_id} | {value[0] for value in before[1].values() if value[0] is not None} | {value[1] for value in before[0].values()}
    printers = {row[0]: row[1] for row in _rows(conn, 'printers', printer_ids, 'id,org_id', lock=True)}
    _rows(conn, 'filament_slots', set(before[0]), 'id', lock=True)
    spools = {row[0]: row for row in _rows(conn, 'spools', set(before[1]), _SPOOL_COLUMNS, lock=True)}
    if _graph(conn, printer_id, tags, uuids, trays) != before or set(printers) != printer_ids:
        raise AMSInventoryError('changed', printer_id, slots=before[0], spools=before[1])
    printer_model = conn.execute('SELECT model,machine_type FROM printers WHERE id=?', (printer_id,)).fetchone()
    org = printers[printer_id]
    if any(value is not None and value != org for value in printers.values()):
        raise AMSInventoryError('unavailable', printer_id, slots=before[0], spools=before[1])
    for slot in before[0].values():
        if slot[0] is not None:
            row = spools[slot[0]]
            location = before[1][row[0]]
            if location[0] is not None and location != (slot[1],slot[2]):
                raise AMSInventoryError('contradictory', printer_id, slots=before[0], spools=(row[0],), numbers=(slot[2],))
    for row in spools.values():
        if row[1] is not None and row[1] != org:
            raise AMSInventoryError('unavailable', printer_id, slots=before[0], spools=before[1])
        if row[2] is not None and printers[row[2]] is not None and printers[row[2]] != org:
            raise AMSInventoryError('unavailable', printer_id, slots=before[0], spools=before[1])
    for spool_id, location in before[3].items():
        conn.execute('INSERT INTO inventory_repairs(event_id,spool_id,action,before_json,after_json) VALUES(?,?,?,?,?)',
                     (str(uuid.uuid4()), spool_id, 'stale-location.clear', json.dumps(location), json.dumps([None, None])))
        conn.execute('UPDATE spools SET location_printer_id=NULL,location_slot=NULL WHERE id=?', (spool_id,))
    # Clear the original association graph before writing any final targets.
    # A sequential clear/write loop loses assignments during valid permutations.
    destinations = {number: rfid.match_id(before[2], tray)
                    for number, tray in identified if tray.get('tray_type')}
    known_ids = [value for value in destinations.values() if value is not None]
    if len(known_ids) != len(set(known_ids)):
        raise AMSInventoryError('ambiguous', printer_id, spools=known_ids)
    used_candidates = set()
    for number, tray in identified:
        if not tray.get('tray_type') or destinations.get(number) is not None or not (tray['tag_uid'] or tray['tray_uuid']):
            continue
        candidates, possible = rfid.choose_candidates(before[4], tray, used_candidates)
        if candidates:
            spool_id = candidates[0]
            used_candidates.add(spool_id)
            conn.execute('UPDATE spools SET tray_uuid=?,rfid_tag=?,color_hex=? WHERE id=?',
                         (tray['tray_uuid'], tray['tag_uid'], (tray.get('tray_color') or '')[:6] or None, spool_id))
            action = 'inventory.rfid_bound'
        else:
            spool_id = rfid.create_raw_spool(conn, org, tray)
            spools[spool_id] = _rows(conn, 'spools', {spool_id}, _SPOOL_COLUMNS)[0]
            action = 'inventory.rfid_created'
        destinations[number] = spool_id
        rfid.raw_audit(conn, action, rfid.audit_details(spool_id, tray, printer_id, number, candidates, possible))
    from core.interfaces.inventory_moves import inventory_moves
    moves = inventory_moves()
    moves.observed_departures(conn, printer_id,
                              [(number, tray.get('tray_type') or '', tray.get('tag_uid')) for number, tray in identified],
                              {value for value in destinations.values() if value is not None})
    for number, spool_id in destinations.items():
        if spool_id is None:
            continue
        for old_id, association in before[0].items():
            if association[0] == spool_id and (association[1], association[2]) != (printer_id, number):
                conn.execute('UPDATE filament_slots SET assigned_spool_id=NULL,spool_confirmed=? WHERE id=?', (False, old_id))
            if (association[1], association[2]) == (printer_id, number) and association[0] is not None and association[0] != spool_id:
                displaced = spools[association[0]]
                if (displaced[2], displaced[5]) == (printer_id, number):
                    conn.execute('UPDATE spools SET location_printer_id=NULL,location_slot=NULL WHERE id=?', (association[0],))
    for number, tray in identified:
        material = tray.get('tray_type') or ''
        color = (tray.get('tray_color') or '')[:6] if material else None
        filament_type = slot_filament_value(material)
        existing = conn.execute('SELECT id,assigned_spool_id FROM filament_slots WHERE printer_id=? AND slot_number=?', (printer_id, number)).fetchone()
        if existing:
            identifier = existing[0]
            conn.execute('UPDATE filament_slots SET filament_type=?,color_hex=?,loaded_at=CURRENT_TIMESTAMP WHERE id=?', (filament_type, color, identifier))
            if not material:
                conn.execute('UPDATE filament_slots SET color=NULL WHERE id=?', (identifier,))
        else:
            conn.execute('INSERT INTO filament_slots(printer_id,slot_number,filament_type,color_hex,loaded_at) VALUES(?,?,?,?,CURRENT_TIMESTAMP)', (printer_id, number, filament_type, color))
            identifier = conn.execute('SELECT id FROM filament_slots WHERE printer_id=? AND slot_number=?', (printer_id, number)).fetchone()[0]
        # Mutation authority is the validated locked snapshot, never a new lookup.
        spool_id = destinations.get(number) if material else None
        if spool_id is not None:
            if not conn.execute("SELECT id FROM audit_logs WHERE entity_type='spool' AND entity_id=? AND action IN ('inventory.rfid_bound','inventory.rfid_created','inventory.rfid_identified','inventory.rfid_merged') LIMIT 1", (spool_id,)).fetchone():
                rfid.raw_audit(conn, 'inventory.rfid_identified', rfid.audit_details(spool_id, tray, printer_id, number))
            if tray['tray_uuid']:
                conn.execute('UPDATE spools SET tray_uuid=? WHERE id=? AND tray_uuid IS NULL', (tray['tray_uuid'], spool_id))
            moves.break_seal(conn, spool_id)
            conn.execute('UPDATE spools SET storage_location=NULL WHERE id=?', (spool_id,))
            conn.execute('UPDATE filament_slots SET assigned_spool_id=?,spool_confirmed=? WHERE id=?', (spool_id, True, identifier))
            row = spools[spool_id]
            initial = row[3]
            percent = remaining_percent(tray.get('remain'))
            weight = rfid.measured_weight(printer_model[0], printer_model[1], percent, initial)
            conn.execute('UPDATE spools SET location_printer_id=?,location_slot=? WHERE id=?', (printer_id, number, spool_id))
            if weight is not None:
                record_ams_measurement(qmark_runner(conn), spool_id, weight, printer_id)
    if not material_incomplete:
        _clear_unplugged_ht(conn, printer_id, reported_slots)
    return len(trays)


def _clear_unplugged_ht(conn, printer_id, reported):
    """Clear AMS HT slots (17+) whose unit is absent from the reported AMS list.

    HT slots exist only through this sync, so an absent unit was unplugged.
    Runs against the POST-sync state: a spool moved out of the HT slot in the
    same report has already been reassigned, and only a spool still located
    in that HT slot loses its location. Slots 1-16 are left alone because
    they double as user-configured slots (slot_count) without an AMS.
    """
    from modules.printers.telemetry.bambu.ams_identity import AMS_HT_FIRST_SLOT
    suffix = ' FOR UPDATE' if getattr(conn, '_postgres', False) else ''
    rows = conn.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- constant fragment: lock suffix selected from the closed set {'', ' FOR UPDATE'}; values are bound
        f'SELECT id,assigned_spool_id,slot_number FROM filament_slots WHERE printer_id=? AND slot_number>=? ORDER BY id{suffix}',
        (printer_id, AMS_HT_FIRST_SLOT)).fetchall()
    for slot_id, assigned, number in rows:
        if number in reported:
            continue
        conn.execute('UPDATE filament_slots SET filament_type=?,color_hex=NULL,color=NULL,assigned_spool_id=NULL,spool_confirmed=? WHERE id=?',
                     (slot_filament_value('empty'), False, slot_id))
        if assigned is not None:
            conn.execute('UPDATE spools SET location_printer_id=NULL,location_slot=NULL'
                         ' WHERE id=? AND location_printer_id=? AND location_slot=?', (assigned, printer_id, number))
