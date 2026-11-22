"""Shared interpretation of stored Bambu humidity levels."""


def humidity_scale(humidity, temperature):
    """Return valid level, dry-to-wet index (1–5), grade (A–E), and sensor temperature."""
    level = int(humidity) if isinstance(humidity, (int, float)) and 1 <= humidity <= 5 else None
    # Older AMS lite rows use level 5 at exactly 0 degrees for an absent sensor.
    if level == 5 and temperature == 0:
        return None, None, None, None
    index = 6 - level if level is not None else None
    return level, index, "ABCDE"[index - 1] if index else None, temperature


def reported_percent(value):
    """Return a whole percentage when the sample has a valid measurement."""
    return round(value) if isinstance(value, (int, float)) and 0 <= value <= 100 else None


def zone_label(zone):
    from itertools import groupby
    slots = zone.get('zone_slots')
    if not isinstance(slots, list) or not slots or any(type(n) is not int or not 0 <= n < 9 for n in slots):
        return None
    ranges = []
    for _, group in groupby(enumerate(sorted({n + 1 for n in slots})), lambda pair: pair[1] - pair[0]):
        run = [n for _, n in group]
        ranges.append(str(run[0]) if len(run) == 1 else f'{run[0]}-{run[-1]}')
    return 'Slots ' + ', '.join(ranges)


def zone_labels(db, printer_ids):
    """Latest reported layout. Scope the layout to the lifetime of the printer row."""
    from sqlalchemy import bindparam, text
    from core.display_json import display_object
    from modules.printers.printer_models import rack_model_identity
    rows = db.execute(text(
        "SELECT p.id,p.model,p.machine_type,c.value FROM printers p JOIN system_config c ON c.key LIKE "
        "'ams.zone.' || CAST(p.id AS TEXT) || '.' || CAST(p.created_at AS TEXT) || '.%' WHERE p.id IN :ids"
    ).bindparams(bindparam('ids', expanding=True)), {'ids': list(printer_ids)})
    result = {}
    for pid, model, machine, value in rows:
        label = display_object(value)
        if label and rack_model_identity(model, machine):
            result.setdefault(pid, {}).update(label)
    return result
