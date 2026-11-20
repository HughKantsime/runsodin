"""Latest printer telemetry, shared by monitor processes and API workers."""
from sqlalchemy import DateTime, column, select, table
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


def live_fields(data):
    """Publish one numeric snapshot, including nulls for unreported measurements."""
    values = {}
    for key in LIVE_FIELDS:
        value = data.get(key)
        valid = type(value) in (int, float) and 0 <= value <= 2**53 - 1
        if valid and key in ("current_layer", "total_layers", "speed_level"):
            valid = int(value) == value
        values[key] = value if valid else None
    return values


def save_snapshot(conn, data):
    printers = table("printers", column("id"), column("created_at", DateTime))
    created_at = conn.scalar(select(printers.c.created_at).where(printers.c.id == data["printer_id"]))
    if created_at is None:
        return
    record = {key: data[key] for key in LIVE_FIELDS}
    record.update(telemetry_at=data["telemetry_at"], printer_created_at=str(created_at))
    config = SystemConfig.__table__
    dialect = postgresql if conn.dialect.name == "postgresql" else sqlite
    insert = dialect.insert(config).values(key=cache_key(data["printer_id"]), value=record)
    conn.execute(insert.on_conflict_do_update(
        index_elements=[config.c.key], set_={"value": insert.excluded.value},
        where=config.c.value["telemetry_at"].as_string() < data["telemetry_at"],
    ))


def enrich_live_values(db, printers, actor):
    targets = {cache_key(p.id): p for p in printers}
    if not targets:
        return
    rows = db.execute(select(SystemConfig.key, SystemConfig.value).where(SystemConfig.key.in_(targets)))
    stored = dict(rows.all())
    restricted = bool(stored) and any_read_restrictions(db)
    for key, printer in targets.items():
        record = stored.get(key)
        if not isinstance(record, dict) or record.get("printer_created_at") != str(printer.created_at):
            record = {}
        data = printer_status(db, printer.id, actor, record) if restricted and record else record
        for wire, field in LIVE_FIELDS.items():
            setattr(printer, field, data.get(wire))
        printer.telemetry_at = data.get("telemetry_at")
