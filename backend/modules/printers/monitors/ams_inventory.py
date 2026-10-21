"""Caller-owned, organization-bound AMS inventory updates for raw daemon DBs."""
import math
from core.material_identity import canonical_reported_material
from modules.printers.telemetry.validators import coerce_int_or_raise


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


def _graph(conn, printer_id, tags):
    slots = conn.execute('SELECT id,assigned_spool_id,printer_id,slot_number FROM filament_slots WHERE printer_id=? ORDER BY id', (printer_id,)).fetchall()
    matches = []
    if tags:
        placeholders = ','.join('?' for _ in tags)
        matches = conn.execute(f'SELECT id,rfid_tag FROM spools WHERE rfid_tag IN ({placeholders})', tuple(tags)).fetchall()  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- only '?' placeholders generated from the bound value count; values are bound
    ids = {row[1] for row in slots if row[1] is not None} | {row[0] for row in matches}
    spools = _rows(conn, 'spools', ids, _SPOOL_COLUMNS)
    if ids:
        placeholders = ','.join('?' for _ in ids)
        previous = conn.execute(f'SELECT id,assigned_spool_id,printer_id,slot_number FROM filament_slots WHERE assigned_spool_id IN ({placeholders})', tuple(sorted(ids))).fetchall()  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- only '?' placeholders generated from the bound value count; values are bound
        slots = list({row[0]: row for row in slots + previous}.values())
    for spool in spools:
        if spool[2] is not None:
            located = conn.execute('SELECT id,assigned_spool_id,printer_id,slot_number FROM filament_slots WHERE printer_id=? AND slot_number=?', (spool[2], spool[5])).fetchone()
            if located is None or located[1] != spool[0]:
                raise ValueError('AMS association contradictory')
            slots = list({row[0]: row for row in slots + [located]}.values())
    if {row[0] for row in spools} != ids:
        raise ValueError('AMS association unavailable')
    return ({row[0]: (row[1],row[2],row[3]) for row in slots}, {row[0]: (row[2],row[5]) for row in spools}, {row[1]: row[0] for row in matches})


def sync_ams_inventory(conn, printer_id, units):
    """No commit: caller rolls back failure and advances throttle after commit.

    Existing rows are locked and rechecked; late absent RFID matches are never
    adopted. Arbitrary writers inserting new associations remain a separate
    concurrency acceptance requirement.
    """
    from modules.printers.telemetry.bambu.ams_identity import ams_trays_by_slot, is_rfid_tag
    identified = ams_trays_by_slot(units, strict=True)
    # The all-zero tag_uid of a non-RFID spool is not an identity.
    identified = [(number, tray if is_rfid_tag(tray.get('tag_uid')) else {k: v for k, v in tray.items() if k != 'tag_uid'})
                  for number, tray in identified]
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
        raise ValueError('AMS destinations ambiguous')
    tags = sorted(set(reported_tags))
    if not getattr(conn, '_postgres', False):
        conn.execute('UPDATE printers SET name=name WHERE id=?', (printer_id,))
    before = _graph(conn, printer_id, tags)
    printer_ids = {printer_id} | {value[0] for value in before[1].values() if value[0] is not None} | {value[1] for value in before[0].values()}
    printers = {row[0]: row[1] for row in _rows(conn, 'printers', printer_ids, 'id,org_id', lock=True)}
    _rows(conn, 'filament_slots', set(before[0]), 'id', lock=True)
    spools = {row[0]: row for row in _rows(conn, 'spools', set(before[1]), _SPOOL_COLUMNS, lock=True)}
    if _graph(conn, printer_id, tags) != before or set(printers) != printer_ids:
        raise ValueError('AMS association changed')
    org = printers[printer_id]
    if any(value is not None and value != org for value in printers.values()):
        raise ValueError('AMS association unavailable')
    for slot in before[0].values():
        if slot[0] is not None:
            row = spools[slot[0]]
            if row[2] is not None and (row[2],row[5]) != (slot[1],slot[2]):
                raise ValueError('AMS association contradictory')
    for row in spools.values():
        if row[1] is not None and row[1] != org:
            raise ValueError('AMS association unavailable')
        if row[2] is not None and printers[row[2]] is not None and printers[row[2]] != org:
            raise ValueError('AMS association unavailable')
    # Clear the original association graph before writing any final targets.
    # A sequential clear/write loop loses assignments during valid permutations.
    destinations = {number: before[2].get(tray.get('tag_uid'))
                    for number, tray in identified if tray.get('tray_type')}
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
    from modules.printers.bambu_integration import map_bambu_filament_type
    for number, tray in identified:
        material = tray.get('tray_type') or ''
        color = (tray.get('tray_color') or '')[:6] if material else None
        filament_type = map_bambu_filament_type(material) if material else 'empty'
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
        spool_id = before[2].get(tray.get('tag_uid')) if material else None
        if spool_id is not None:
            conn.execute('UPDATE filament_slots SET assigned_spool_id=?,spool_confirmed=? WHERE id=?', (spool_id, True, identifier))
            row = spools[spool_id]
            initial = row[3]
            percent = remaining_percent(tray.get('remain'))
            weight = None
            if percent is not None and isinstance(initial, (int, float)) and not isinstance(initial, bool):
                try:
                    if math.isfinite(initial) and initial >= 0:
                        computed = initial * (percent / 100)
                        if math.isfinite(computed):
                            weight = computed
                except OverflowError:
                    pass
            if weight is None:
                conn.execute('UPDATE spools SET location_printer_id=?,location_slot=? WHERE id=?', (printer_id, number, spool_id))
            else:
                conn.execute('UPDATE spools SET location_printer_id=?,location_slot=?,remaining_weight_g=? WHERE id=?', (printer_id, number, weight, spool_id))
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
                     ('empty', False, slot_id))
        if assigned is not None:
            conn.execute('UPDATE spools SET location_printer_id=NULL,location_slot=NULL'
                         ' WHERE id=? AND location_printer_id=? AND location_slot=?', (assigned, printer_id, number))
