"""Shared RFID identity and sealed-stock policy; callers own locks/transactions."""
import json
import uuid
import math
from core.log_redaction import redact_structure
from core.filament_ledger import create_rfid_spool, record_ams_measurement
from core.print_attempts import qmark_runner, sqlalchemy_runner
from core.material_identity import canonical_reported_material
from core.models import SystemConfig
from modules.inventory.models import Spool, FilamentLibrary
from modules.printers.models import FilamentSlot
from modules.inventory.receiving import seal_key
from modules.printers.telemetry.bambu.ams_identity import is_rfid_tag

# Observed SKU/name pairs from captured tags, not a hex-distance guess:
# https://github.com/queengooborg/Bambu-Lab-RFID-Library (2026-10-27).
# Ambiguous observed codes (e.g. A01-A2) deliberately have no catalog entry.
BAMBU_COLORS = {
    'A00-W01': 'Jade White', 'A00-W1': 'Jade White',
    'A00-K00': 'Black', 'A00-K0': 'Black',
    'A00-D00': 'Gray', 'A00-D0': 'Gray',
    'A00-D01': 'Silver', 'A00-D1': 'Silver',
    'A00-R00': 'Red', 'A00-R0': 'Red',
    'A00-Y04': 'Gold', 'A00-Y4': 'Gold',
    'A00-D02': 'Light Gray', 'A00-D2': 'Light Gray',
    'A00-P01': 'Pink', 'A00-P1': 'Pink',
    'A00-A00': 'Orange',  # A00-A0 and A00-A1 are ambiguous in the source.
    'A00-B08': 'Cyan', 'A00-B8': 'Cyan',
    'G02-R0': 'Red', 'G02-D0': 'Gray', 'G02-D1': 'Dark Gray',
    'G02-G2': 'Forest Green', 'A19-P00': 'Milky Pink',
    'G01-C0': 'Clear', 'A05-M1': 'South Beach',
    # PLA Matte Baby Blue is not listed; do not borrow another line's code.
    'A00-B09': 'Blue', 'A00-B9': 'Blue',
    'A01-D0': 'Nardo Gray', 'A01-D03': 'Ash Gray', 'A01-D3': 'Ash Gray',
    'A01-W02': 'Ivory White', 'A01-W2': 'Ivory White', 'A01-W3': 'Bone White',
    'A01-K01': 'Charcoal', 'A01-K1': 'Charcoal',
    'A01-B0': 'Sky Blue', 'A01-B3': 'Marine Blue', 'A01-B4': 'Ice Blue', 'A01-B6': 'Dark Blue',
    'A01-G0': 'Apple Green', 'A01-G1': 'Grass Green', 'A01-G7': 'Dark Green',
    'A01-N0': 'Dark Chocolate', 'A01-N1': 'Latte Brown', 'A01-N2': 'Dark Brown', 'A01-N3': 'Caramel',
    'A01-P03': 'Sakura Pink', 'A01-P3': 'Sakura Pink', 'A01-P4': 'Lilac Purple',
    'A01-R1': 'Scarlet Red', 'A01-R2': 'Terracotta', 'A01-R3': 'Plum', 'A01-R4': 'Dark Red',
    'A01-Y2': 'Lemon Yellow', 'A01-Y3': 'Desert Tan',
}


def identity(value):
    return value.strip() if is_rfid_tag(value) else None


def color_name(tray):
    return BAMBU_COLORS.get((tray.get('tray_id_name') or '').strip().upper())


def slot_tray(slot):
    return {'tag_uid': identity(slot.rfid_tag), 'tray_uuid': identity(getattr(slot, 'tray_uuid', None)),
            'tray_id_name': getattr(slot, 'tray_id_name', ''), 'tray_type': slot.filament_type,
            'tray_sub_brands': slot.sub_brand, 'tray_color': slot.color_hex, 'remain': slot.remaining_percent}


def match_id(matches, tray):
    return matches.get('uuid:' + (identity(tray.get('tray_uuid')) or '')) or matches.get(identity(tray.get('tag_uid')))


def candidate_rows(db, org):
    """Snapshot includes color/library facts; recheck it after graph locks."""
    seals = {r.key for r in db.query(SystemConfig).filter(SystemConfig.key.startswith('inventory.sealed.')).all() if r.value is True}
    rows = db.query(Spool.id, Spool.color_name, FilamentLibrary.name, FilamentLibrary.material, FilamentLibrary.brand).join(
        FilamentLibrary, Spool.filament_id == FilamentLibrary.id).filter(
        Spool.org_id == org, Spool.status == 'active', Spool.rfid_tag.is_(None), Spool.tray_uuid.is_(None),
        Spool.location_printer_id.is_(None), Spool.location_slot.is_(None),
        ~Spool.id.in_(db.query(FilamentSlot.assigned_spool_id).filter(
            FilamentSlot.assigned_spool_id.isnot(None)))).order_by(Spool.id).all()
    return [tuple(r) for r in rows if seal_key(r[0]) in seals]


def raw_candidate_rows(conn, org):
    rows = conn.execute('''SELECT s.id,s.color_name,f.name,f.material,f.brand,c.value
        FROM spools s JOIN filament_library f ON f.id=s.filament_id
        JOIN system_config c ON c.key='inventory.sealed.' || CAST(s.id AS VARCHAR)
        WHERE (s.org_id=? OR (s.org_id IS NULL AND CAST(? AS INTEGER) IS NULL))
        AND s.status='active' AND s.rfid_tag IS NULL AND s.tray_uuid IS NULL
        AND s.location_printer_id IS NULL AND s.location_slot IS NULL
        AND NOT EXISTS (SELECT 1 FROM filament_slots fs WHERE fs.assigned_spool_id=s.id)
        ORDER BY s.id''', (org, org)).fetchall()
    return [tuple(r[:5]) for r in rows if r[5] is True or r[5] == 'true']


def choose_candidates(rows, tray, used=()):
    material = canonical_reported_material(tray.get('tray_type'))
    if not material:
        return [], []
    line = tray.get('tray_sub_brands') or tray.get('tray_type')
    rows = [r for r in rows if r[0] not in used and r[4].casefold() == 'bambu lab'
            and r[2] == line and canonical_reported_material(r[3]) == material]
    name = color_name(tray)
    if name:
        return [r[0] for r in rows if (r[1] or '').strip().casefold() == name.casefold()], []
    colors = {(r[1] or '').strip().casefold() for r in rows}
    if len(colors) == 1 and '' not in colors:
        return [r[0] for r in rows], []
    return [], [r[0] for r in rows]


def audit_details(spool_id, tray, printer_id, slot, candidates=(), possible=()):
    result = dict(spool_id=spool_id, tray_uuid=identity(tray.get('tray_uuid')), printer_id=printer_id,
                  slot=slot, candidates=len(candidates))
    if possible:
        result.update(possible_duplicate=True, possible_duplicate_spool_ids=list(possible))
    return result


def raw_audit(conn, action, details):
    # Raw monitor audits share the business transaction, like inventory repairs.
    # No detached ORM session or independent commit; the monitor commits both.
    conn.execute('INSERT INTO audit_logs(action,entity_type,entity_id,details) VALUES(?,?,?,?)',
                 (action, 'spool', details['spool_id'], json.dumps(redact_structure(details))))


def create_raw_spool(conn, org, tray):
    material = canonical_reported_material(tray.get('tray_type'))
    line = tray.get('tray_sub_brands') or tray.get('tray_type')
    library = conn.execute("SELECT id FROM filament_library WHERE brand='Bambu Lab' AND name=? AND material=? ORDER BY id LIMIT 1", (line, material)).fetchone()
    if library is None:
        library = conn.execute("INSERT INTO filament_library(brand,name,material,color_hex) VALUES('Bambu Lab',?,?,?) RETURNING id", (line, material, (tray.get('tray_color') or '')[:6] or None)).fetchone()
    return create_rfid_spool(qmark_runner(conn), filament_id=library[0],
        qr_code='SPL-' + uuid.uuid4().hex[:8].upper(), tag=identity(tray.get('tag_uid')),
        tray_uuid=identity(tray.get('tray_uuid')), color_hex=(tray.get('tray_color') or '')[:6] or None,
        color_name=color_name(tray), org_id=org)


class LockedMatches(dict):
    def __init__(self, matches, candidates):
        super().__init__(matches)
        self.candidates = candidates


def measures_remaining(model, machine_type=None):
    """A nonempty machine_type is authoritative; never infer from remain%."""
    value = machine_type or model or ''
    normalized = ''.join(c for c in value.upper() if c.isalnum())
    for prefix in ('BAMBULAB', 'BAMBU'):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
            break
    return normalized in {'X1C', 'X1', 'X1E', 'H2D', 'X1CARBON',
                          'BLP001', 'BLP002', 'C13', 'O1D'}


def measured_weight(model, machine_type, percent, initial):
    if not measures_remaining(model, machine_type):
        return None
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (percent, initial)):
        return None
    try:
        if math.isfinite(percent) and 0 <= percent <= 100 and math.isfinite(initial) and initial >= 0:
            weight = initial * (percent / 100)
            return weight if math.isfinite(weight) else None
    except OverflowError:
        pass
    return None


def accept_measurement(db, spool, printer, percent):
    weight = measured_weight(printer.model, printer.machine_type, percent, spool.initial_weight_g)
    if weight is not None:
        db.flush()
        record_ams_measurement(sqlalchemy_runner(db), spool.id, weight, printer.id)
        db.expire(spool, ['remaining_weight_g', 'remaining_measured_g',
                          'remaining_measured_at', 'remaining_measured_printer_id'])
