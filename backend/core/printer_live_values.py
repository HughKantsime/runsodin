"""Latest printer telemetry, shared by monitor processes and API workers."""
from datetime import datetime, timezone
import json

from sqlalchemy import DateTime, JSON, Text, case, cast, column, func, select, table
from sqlalchemy.dialects import postgresql, sqlite

from core.controlled_parts import any_read_restrictions
from core.controlled_redaction import printer_status
from core.models import SystemConfig

LIVE_FIELDS = {
    "progress": "mc_percent", "remaining_min": "mc_remaining_time",
    "current_layer": "layer_num", "total_layers": "total_layer_num",
    "speed_level": "speed_level", "speed_percent": "speed_percent",
}


def cache_key(printer_id):
    return f"printer.live_values.{printer_id}"


def _valid_field(key, value):
    if value is None:
        return True
    if type(value) not in (int, float) or not 0 <= value <= 2**53 - 1:
        return False
    if key == "progress":
        return value <= 100
    if key == "speed_level":
        return value in (1, 2, 3, 4)
    return key not in ("current_layer", "total_layers") or int(value) == value


def live_fields(data):
    """Only an explicit null clears a measurement."""
    return {k: data[k] for k in LIVE_FIELDS if k in data and _valid_field(k, data[k])}


def save_snapshot(conn, data):
    printers = table("printers", column("id"), column("created_at", DateTime))
    printer = conn.execute(select(printers.c.created_at).where(printers.c.id == data["printer_id"])).first()
    if printer is None:
        return
    # Legacy rows with no creation date use the printer ID in the cache key.
    identity = str(printer.created_at)
    patch = {k: data[k] for k in LIVE_FIELDS if k in data}
    patch.update(telemetry_at=data["telemetry_at"], printer_created_at=identity)
    record = dict.fromkeys(LIVE_FIELDS) | patch
    config = SystemConfig.__table__
    previous = config.c.value
    if conn.dialect.name == "postgresql":
        insert = postgresql.insert(config)
        merged = cast(cast(previous, postgresql.JSONB).concat(patch), JSON)
    else:
        insert = sqlite.insert(config)
        args = [item for key, value in patch.items() for item in (f"$.{key}", value)]
        merged = func.json_set(previous, *args)
    insert = insert.values(key=cache_key(data["printer_id"]), value=record)
    same_printer = previous["printer_created_at"].as_string() == identity
    conn.execute(insert.on_conflict_do_update(
        index_elements=[config.c.key],
        set_={"value": case((same_printer, merged), else_=insert.excluded.value)},
        where=previous["telemetry_at"].as_string() < data["telemetry_at"],
    ))


def _read_snapshot(raw, created_at):
    try:
        record = json.loads(raw)
        if not isinstance(record, dict) or record.get("printer_created_at") != str(created_at):
            return {}
        if not all(_valid_field(key, record[key]) for key in LIVE_FIELDS):
            return {}
        stamp = datetime.fromisoformat(record["telemetry_at"])
        if stamp.tzinfo is None:
            return {}
        record["telemetry_at"] = stamp.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        return record
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return {}


def enrich_live_values(db, printers, actor):
    targets = {cache_key(p.id): p for p in printers}
    if not targets:
        return
    rows = db.execute(select(SystemConfig.key, cast(SystemConfig.value, Text)).where(SystemConfig.key.in_(targets)))
    stored = dict(rows.all())
    restricted = bool(stored) and any_read_restrictions(db)
    for key, printer in targets.items():
        record = _read_snapshot(stored[key], printer.created_at) if key in stored else {}
        data = printer_status(db, printer.id, actor, record) if restricted and record else record
        for wire, field in LIVE_FIELDS.items():
            setattr(printer, field, data.get(wire))
        printer.telemetry_at = data.get("telemetry_at")
