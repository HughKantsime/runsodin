"""Repair only the extra empty rows created by counting AMS HT trays as standard trays."""
import json
import logging
import re

from modules.printers.telemetry.bambu.ams_identity import ams_slot_number, ams_trays_by_slot


def full_ams_slots(packet):
    """Accept a complete unit roster, never a bit-only or partial tray update."""
    if packet.get('msg') not in (None, 0, '0'):
        return None
    ams = packet.get('ams')
    units = ams.get('ams') if isinstance(ams, dict) else None
    if not isinstance(units, list):
        return None
    mask = ams.get('ams_exist_bits')
    if mask is None:
        return None
    try:
        numbers = {n for n, _ in ams_trays_by_slot(units, strict=True)}
    except ValueError:
        return None
    unit_bits = 0
    expected = set()
    for unit in units:
        first = ams_slot_number(unit.get('id'), 0)
        if first is None or first in expected:
            return None
        expected.update(range(first, first + (4 if first <= 16 else 1)))
        unit_bits |= 1 << ((first - 1) // 4 if first <= 16 else first - 17 + 4)
    if not isinstance(mask, str) or not re.fullmatch('[0-9a-fA-F]{1,8}', mask) or int(mask, 16) != unit_bits:
        return None
    return numbers if numbers == expected else None


def _has_repair_history(run, printer_id, rows):
    """Older inventory repairs reference either a slot row or a physical location."""
    ids = {slot_id for slot_id, _ in rows}
    locations = {(printer_id, number) for _, number in rows}
    repairs = run("""SELECT action,before_json,after_json FROM inventory_repairs
                     WHERE action='admin.resolve' OR CAST(before_json AS TEXT) LIKE :printer""",
                  {'printer': f'%{printer_id}%'})
    for action, before, after in repairs:
        snapshot = json.loads(before)
        if action == 'admin.resolve':
            for state in (snapshot, json.loads(after)):
                if any(item['filament_slot_id'] in ids for item in state['contradictions']):
                    return True
                if any(ids.intersection(item['filament_slot_ids']) for item in state['duplicates']):
                    return True
        elif action == 'stale-location.clear':
            if tuple(snapshot) in locations:
                return True
        elif (snapshot.get('printer_id'), snapshot.get('slot')) in locations:
            return True
    return False


# Empty rows also get a load timestamp when created. A later timestamp records an edit.
_DELETE_UNUSED_SLOT = """DELETE FROM filament_slots WHERE id=:id AND printer_id=:printer AND slot_number=:number
    AND (loaded_at IS NULL OR loaded_at=(SELECT created_at FROM printers WHERE id=:printer))
    AND tray_observation IS NULL
    AND filament_type='empty' AND color IS NULL AND color_hex IS NULL
    AND assigned_spool_id IS NULL AND spoolman_spool_id IS NULL AND spool_confirmed IS NOT TRUE
    AND do_not_feed IS NOT TRUE AND do_not_feed_note IS NULL
    AND NOT EXISTS (SELECT 1 FROM spools WHERE location_printer_id=:printer AND location_slot=:number)
    AND NOT EXISTS (SELECT 1 FROM tray_confirmations WHERE slot_id=:id)
    AND NOT EXISTS (SELECT 1 FROM ams_report_trays t
        JOIN ams_report_packets p ON p.packet_sequence=t.packet_sequence
        JOIN ams_read_generations g ON g.generation_id=p.generation_id
        WHERE g.printer_id=:printer AND t.slot_number=:number)
    AND NOT EXISTS (SELECT 1 FROM audit_logs WHERE entity_type='filament_slot' AND entity_id=:id)
    AND NOT EXISTS (SELECT 1 FROM audit_logs WHERE entity_type='spool'
        AND CAST(details->>'printer_id' AS TEXT)=CAST(:printer AS TEXT)
        AND CAST(details->>'slot' AS TEXT)=CAST(:number AS TEXT)) RETURNING id"""


def _remove_unused_rows(run, params):
    rows = run("""SELECT id,slot_number FROM filament_slots WHERE printer_id=:printer
                  AND slot_number>:standard AND slot_number<=:old ORDER BY slot_number""", params).fetchall()
    if rows and _has_repair_history(run, params['printer'], rows):
        return False
    for slot_id, number in rows:
        if not run(_DELETE_UNUSED_SLOT, {**params, 'id': slot_id, 'number': number}).fetchone():
            return False
    return True


def repair_phantom_slots(run, printer_id, numbers):
    """Repair as one optional part of the caller's inventory transaction."""
    if not numbers:
        return
    standard = sum(n <= 16 for n in numbers)
    ht = len(numbers) - standard
    if not standard or not ht:
        return
    params = {'printer': printer_id, 'standard': standard, 'maximum': standard + ht}
    row = run("""SELECT slot_count FROM printers WHERE id=:printer AND lower(api_type)='bambu'
                 AND slot_count>:standard AND slot_count<=:maximum""", params).fetchone()
    if not row or numbers.intersection(range(standard + 1, row[0] + 1)):
        return
    params['old'] = row[0]
    run('SAVEPOINT phantom_slot_repair', {})
    try:
        locked = run("""UPDATE printers SET slot_count=:standard WHERE id=:printer
                        AND lower(api_type)='bambu' AND slot_count=:old RETURNING id""", params).fetchone()
        if locked and _remove_unused_rows(run, params):
            run("""INSERT INTO audit_logs(action,entity_type,entity_id,details)
                   VALUES('printer.observed_config_changed','printer',:printer,:details)""",
                {'printer': printer_id, 'details': json.dumps({'field': 'slot_count', 'old': row[0], 'new': standard})})
        else:
            run('ROLLBACK TO SAVEPOINT phantom_slot_repair', {})
    except Exception:
        run('ROLLBACK TO SAVEPOINT phantom_slot_repair', {})
        logging.getLogger(__name__).warning("Could not repair extra AMS slots on printer %s", printer_id)
    finally:
        run('RELEASE SAVEPOINT phantom_slot_repair', {})
