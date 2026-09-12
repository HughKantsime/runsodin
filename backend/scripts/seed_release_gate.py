"""Seed the isolated full-stack candidate-gate database.

This command is intentionally unusable without an exact caller-provided run marker.
It never wipes data and refuses databases containing non-gate users or conflicting
fixture identities.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from pathlib import Path

try:
    from .demo_seed_edu import Persona, upsert_personas_connection
except ImportError:  # Direct execution from /app/backend/scripts.
    from demo_seed_edu import Persona, upsert_personas_connection


ADMIN_EMAIL = "candidate-admin@example.invalid"
OPERATOR_EMAIL = "candidate-operator@example.invalid"
VIEWER_EMAIL = "candidate-viewer@example.invalid"
PRINTER_NAME = "ODIN Candidate Gate Printer"
MODEL_NAME = "ODIN Candidate Calibration Cube"
PRODUCT_SKU = "ODIN-CANDIDATE-001"
ORDER_NUMBER = "ODIN-CANDIDATE-ORDER-001"
RUN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")

_TABLE_INFO_SQL = {
    "users": "PRAGMA table_info(users)",
    "system_config": "PRAGMA table_info(system_config)",
    "printers": "PRAGMA table_info(printers)",
    "filament_slots": "PRAGMA table_info(filament_slots)",
    "filament_library": "PRAGMA table_info(filament_library)",
    "spools": "PRAGMA table_info(spools)",
    "models": "PRAGMA table_info(models)",
    "products": "PRAGMA table_info(products)",
    "product_components": "PRAGMA table_info(product_components)",
    "orders": "PRAGMA table_info(orders)",
    "order_items": "PRAGMA table_info(order_items)",
    "jobs": "PRAGMA table_info(jobs)",
    "alerts": "PRAGMA table_info(alerts)",
    "vision_detections": "PRAGMA table_info(vision_detections)",
}

_IDENTITY_SQL = {
    ("filament_library", "brand = ? AND name = ?"): (
        "SELECT id FROM filament_library WHERE brand = ? AND name = ?"
    ),
    ("printers", "name = ?"): "SELECT id FROM printers WHERE name = ?",
    ("spools", "qr_code = ?"): "SELECT id FROM spools WHERE qr_code = ?",
    ("models", "name = ?"): "SELECT id FROM models WHERE name = ?",
    ("products", "sku = ?"): "SELECT id FROM products WHERE sku = ?",
    ("orders", "order_number = ?"): "SELECT id FROM orders WHERE order_number = ?",
    ("jobs", "notes = ?"): "SELECT id FROM jobs WHERE notes = ?",
    ("alerts", "title = ?"): "SELECT id FROM alerts WHERE title = ?",
    ("vision_detections", "metadata_json = ?"): (
        "SELECT id FROM vision_detections WHERE metadata_json = ?"
    ),
}

_POPULATION_COUNT_SQL = (
    ("printers", "SELECT COUNT(*) FROM printers"),
    ("filament_library", "SELECT COUNT(*) FROM filament_library"),
    ("spools", "SELECT COUNT(*) FROM spools"),
    ("models", "SELECT COUNT(*) FROM models"),
    ("products", "SELECT COUNT(*) FROM products"),
    ("orders", "SELECT COUNT(*) FROM orders"),
    ("jobs", "SELECT COUNT(*) FROM jobs"),
    ("alerts", "SELECT COUNT(*) FROM alerts"),
    ("vision_detections", "SELECT COUNT(*) FROM vision_detections"),
)


class SeedSafetyError(RuntimeError):
    """The target database does not satisfy release-gate safety invariants."""


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    try:
        statement = _TABLE_INFO_SQL[table]
    except KeyError as exc:
        raise SeedSafetyError("release-gate schema requested an unknown table") from exc
    return {str(row[1]) for row in connection.execute(statement)}


def _require_schema(connection: sqlite3.Connection) -> None:
    required = {
        "users": {"id", "username", "email", "password_hash", "role"},
        "system_config": {"key", "value"},
        "printers": {"id", "name", "is_active", "api_host", "api_key", "camera_url"},
        "filament_slots": {"id", "printer_id", "slot_number", "assigned_spool_id"},
        "filament_library": {"id", "brand", "name"},
        "spools": {"id", "filament_id", "qr_code"},
        "models": {"id", "name"},
        "products": {"id", "name", "sku"},
        "product_components": {"id", "product_id", "model_id"},
        "orders": {"id", "order_number"},
        "order_items": {"id", "order_id", "product_id"},
        "jobs": {"id", "item_name", "notes"},
        "alerts": {"id", "user_id", "title"},
        "vision_detections": {"id", "printer_id", "metadata_json"},
    }
    problems: list[str] = []
    for table, columns in required.items():
        missing = columns - _table_columns(connection, table)
        if missing:
            problems.append(f"{table} missing {','.join(sorted(missing))}")
    if problems:
        raise SeedSafetyError("release-gate schema is incomplete: " + "; ".join(problems))


def _single_id(
    connection: sqlite3.Connection,
    table: str,
    where: str,
    params: tuple[object, ...],
) -> int | None:
    try:
        statement = _IDENTITY_SQL[(table, where)]
    except KeyError as exc:
        raise SeedSafetyError("release-gate fixture requested an unknown identity query") from exc
    rows = connection.execute(statement, params).fetchall()
    if len(rows) > 1:
        raise SeedSafetyError(f"conflicting {table} fixture identity")
    return int(rows[0][0]) if rows else None


def _insert_id(connection: sqlite3.Connection, sql: str, params: tuple[object, ...]) -> int:
    cursor = connection.execute(sql, params)
    return int(cursor.lastrowid)


def _marker_value(connection: sqlite3.Connection) -> str | None:
    row = connection.execute(
        "SELECT value FROM system_config WHERE key = 'release_gate_run_id'"
    ).fetchone()
    if not row:
        return None
    try:
        value = json.loads(row[0])
    except (TypeError, json.JSONDecodeError) as exc:
        raise SeedSafetyError("release-gate database marker is malformed") from exc
    return value if isinstance(value, str) else None


def _validate_target(
    connection: sqlite3.Connection,
    run_id: str,
    marker: str,
    allowed_users: set[str],
) -> None:
    if not RUN_ID_PATTERN.fullmatch(run_id) or marker != run_id:
        raise SeedSafetyError("exact valid release-gate run marker is required")
    _require_schema(connection)
    users = {
        str(row[0])
        for row in connection.execute("SELECT username FROM users").fetchall()
    }
    unexpected = sorted(users - allowed_users)
    if unexpected:
        raise SeedSafetyError("unexpected users in release-gate database")
    if ADMIN_EMAIL not in users:
        raise SeedSafetyError("fresh setup admin must exist before fixture seeding")
    existing_marker = _marker_value(connection)
    if existing_marker is not None and existing_marker != run_id:
        raise SeedSafetyError("release-gate database belongs to another run")
    if existing_marker is None:
        populated = []
        for table, statement in _POPULATION_COUNT_SQL:
            if int(connection.execute(statement).fetchone()[0]):
                populated.append(table)
        if populated:
            raise SeedSafetyError(
                "unmarked release-gate database already contains domain data: "
                + ", ".join(populated)
            )


def _ensure_personas(
    connection: sqlite3.Connection,
    admin_email: str,
    admin_password: str,
    operator_email: str,
    operator_password: str,
    viewer_email: str,
    viewer_password: str,
) -> dict[str, int]:
    personas = [
        Persona(admin_email, admin_password, "admin", no_mfa=True),
        Persona(operator_email, operator_password, "operator", no_mfa=True),
        Persona(viewer_email, viewer_password, "viewer", no_mfa=True),
    ]
    upsert_personas_connection(connection, personas)
    rows = connection.execute(
        "SELECT id, username, role FROM users ORDER BY username"
    ).fetchall()
    expected = {admin_email: "admin", operator_email: "operator", viewer_email: "viewer"}
    actual = {str(row[1]): str(row[2]) for row in rows}
    if actual != expected:
        raise SeedSafetyError("release-gate persona roles do not match expected roles")
    return {str(row[1]): int(row[0]) for row in rows}


def _ensure_domain_graph(
    connection: sqlite3.Connection, run_id: str, user_ids: dict[str, int]
) -> None:
    marker = f"odin-candidate-gate:{run_id}"

    filament_id = _single_id(
        connection, "filament_library", "brand = ? AND name = ?", ("ODIN Candidate", "Gate PLA")
    )
    if filament_id is None:
        filament_id = _insert_id(
            connection,
            "INSERT INTO filament_library (brand, name, material, color_hex, cost_per_gram, is_custom) "
            "VALUES (?, ?, 'PLA', 'C47A1A', 0.025, 1)",
            ("ODIN Candidate", "Gate PLA"),
        )

    printer_id = _single_id(connection, "printers", "name = ?", (PRINTER_NAME,))
    printer_values = (
        PRINTER_NAME, "Candidate", "Synthetic / No Hardware", 1, 0, 0,
        None, None, None, 0, None, None, None, None, "idle", '["candidate-gate"]', 0, 1,
    )
    if printer_id is None:
        printer_id = _insert_id(
            connection,
            "INSERT INTO printers (name, nickname, model, slot_count, is_active, camera_enabled, "
            "api_type, api_host, api_key, camera_discovered, camera_url, plug_host, plug_auth_token, "
            "plug_entity_id, gcode_state, tags, timelapse_enabled, shared) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            printer_values,
        )
    else:
        connection.execute(
            "UPDATE printers SET nickname=?, model=?, slot_count=?, is_active=?, camera_enabled=?, "
            "api_type=?, api_host=?, api_key=?, camera_discovered=?, camera_url=?, plug_host=?, "
            "plug_auth_token=?, plug_entity_id=?, gcode_state=?, tags=?, timelapse_enabled=?, shared=? WHERE id=?",
            printer_values[1:] + (printer_id,),
        )

    spool_id = _single_id(connection, "spools", "qr_code = ?", ("ODIN-CANDIDATE-SPOOL",))
    if spool_id is None:
        spool_id = _insert_id(
            connection,
            "INSERT INTO spools (filament_id, qr_code, color_hex, initial_weight_g, remaining_weight_g, "
            "spool_weight_g, price, vendor, status, location_printer_id, location_slot, storage_location, notes) "
            "VALUES (?, ?, 'C47A1A', 1000, 750, 250, 24.99, 'ODIN Candidate', 'active', ?, 1, 'Candidate Lab', ?)",
            (filament_id, "ODIN-CANDIDATE-SPOOL", printer_id, marker),
        )
    else:
        connection.execute(
            "UPDATE spools SET filament_id=?, location_printer_id=?, location_slot=1, status='active', notes=? WHERE id=?",
            (filament_id, printer_id, marker, spool_id),
        )

    slot_rows = connection.execute(
        "SELECT id FROM filament_slots WHERE printer_id=? AND slot_number=1", (printer_id,)
    ).fetchall()
    if len(slot_rows) > 1:
        raise SeedSafetyError("conflicting candidate filament slot")
    if slot_rows:
        connection.execute(
            "UPDATE filament_slots SET filament_type='PLA', color='amber', color_hex='#C47A1A', "
            "assigned_spool_id=?, spool_confirmed=1 WHERE id=?",
            (spool_id, int(slot_rows[0][0])),
        )
    else:
        connection.execute(
            "INSERT INTO filament_slots (printer_id, slot_number, filament_type, color, color_hex, assigned_spool_id, spool_confirmed) "
            "VALUES (?, 1, 'PLA', 'amber', '#C47A1A', ?, 1)",
            (printer_id, spool_id),
        )

    model_id = _single_id(connection, "models", "name = ?", (MODEL_NAME,))
    if model_id is None:
        model_id = _insert_id(
            connection,
            "INSERT INTO models (name, build_time_hours, default_filament_type, color_requirements, "
            "category, notes, cost_per_item, units_per_bed, quantity_per_bed, markup_percent) "
            "VALUES (?, 0.5, 'PLA', ?, 'Calibration', ?, 0.42, 1, 1, 200)",
            (MODEL_NAME, '{"slot_1":{"color":"amber","grams":16}}', marker),
        )

    product_id = _single_id(connection, "products", "sku = ?", (PRODUCT_SKU,))
    if product_id is None:
        product_id = _insert_id(
            connection,
            "INSERT INTO products (name, sku, price, description) VALUES (?, ?, 4.99, ?)",
            ("ODIN Candidate Classroom Kit", PRODUCT_SKU, marker),
        )
    component_rows = connection.execute(
        "SELECT id FROM product_components WHERE product_id=? AND model_id=?",
        (product_id, model_id),
    ).fetchall()
    if len(component_rows) > 1:
        raise SeedSafetyError("conflicting candidate product component")
    if not component_rows:
        connection.execute(
            "INSERT INTO product_components (product_id, model_id, quantity_needed, notes) VALUES (?, ?, 1, ?)",
            (product_id, model_id, marker),
        )

    order_id = _single_id(connection, "orders", "order_number = ?", (ORDER_NUMBER,))
    if order_id is None:
        order_id = _insert_id(
            connection,
            "INSERT INTO orders (order_number, platform, customer_name, customer_email, status, revenue, notes) "
            "VALUES (?, 'education', 'Candidate Student', 'student@example.invalid', 'pending', 4.99, ?)",
            (ORDER_NUMBER, marker),
        )
    order_item_rows = connection.execute(
        "SELECT id FROM order_items WHERE order_id=? AND product_id=?", (order_id, product_id)
    ).fetchall()
    if len(order_item_rows) > 1:
        raise SeedSafetyError("conflicting candidate order item")
    if order_item_rows:
        order_item_id = int(order_item_rows[0][0])
    else:
        order_item_id = _insert_id(
            connection,
            "INSERT INTO order_items (order_id, product_id, quantity, unit_price, fulfilled_quantity) "
            "VALUES (?, ?, 1, 4.99, 0)",
            (order_id, product_id),
        )

    for suffix, status, assigned_printer in (
        ("pending", "pending", None),
        ("completed", "completed", printer_id),
    ):
        note = f"{marker}:{suffix}"
        job_id = _single_id(connection, "jobs", "notes = ?", (note,))
        if job_id is None:
            _insert_id(
                connection,
                "INSERT INTO jobs (model_id, item_name, quantity, status, priority, printer_id, duration_hours, "
                "colors_required, filament_type, notes, estimated_cost, suggested_price, order_item_id, "
                "quantity_on_bed, submitted_by, charged_to_user_id, required_tags, target_type, queue_position, "
                "hold, is_locked) "
                "VALUES (?, ?, 1, ?, 3, ?, 0.5, 'amber', 'PLA', ?, 0.42, 4.99, ?, 1, ?, ?, '[]', "
                "'specific', ?, 0, 0)",
                (
                    model_id, f"ODIN Candidate Cube ({suffix})", status, assigned_printer, note,
                    order_item_id, user_ids[VIEWER_EMAIL], user_ids[VIEWER_EMAIL],
                    1 if status == "pending" else None,
                ),
            )
        else:
            connection.execute(
                "UPDATE jobs SET hold=0, is_locked=0, required_tags='[]', target_type='specific' "
                "WHERE id=?",
                (job_id,),
            )

    alert_id = _single_id(connection, "alerts", "title = ?", ("ODIN Candidate Gate Alert",))
    if alert_id is None:
        _insert_id(
            connection,
            "INSERT INTO alerts (user_id, alert_type, severity, title, message, is_read, is_dismissed, printer_id, metadata_json) "
            "VALUES (?, 'printer_error', 'warning', 'ODIN Candidate Gate Alert', ?, 0, 0, ?, ?)",
            (user_ids[ADMIN_EMAIL], "Synthetic candidate evidence", printer_id, json.dumps({"marker": marker})),
        )

    detection_id = _single_id(
        connection, "vision_detections", "metadata_json = ?", (json.dumps({"marker": marker}),)
    )
    if detection_id is None:
        _insert_id(
            connection,
            "INSERT INTO vision_detections (printer_id, detection_type, confidence, status, metadata_json) "
            "VALUES (?, 'spaghetti', 0.91, 'pending', ?)",
            (printer_id, json.dumps({"marker": marker})),
        )

    connection.execute(
        "INSERT INTO system_config (key, value) VALUES ('release_gate_run_id', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (json.dumps(run_id),),
    )


def _manifest(connection: sqlite3.Connection, run_id: str) -> dict[str, int]:
    marker = f"odin-candidate-gate:{run_id}"
    counts = {
        "users": int(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]),
        "printers": int(connection.execute("SELECT COUNT(*) FROM printers WHERE name=?", (PRINTER_NAME,)).fetchone()[0]),
        "filament_slots": int(connection.execute("SELECT COUNT(*) FROM filament_slots fs JOIN printers p ON p.id=fs.printer_id WHERE p.name=?", (PRINTER_NAME,)).fetchone()[0]),
        "filaments": int(connection.execute("SELECT COUNT(*) FROM filament_library WHERE brand='ODIN Candidate' AND name='Gate PLA'").fetchone()[0]),
        "spools": int(connection.execute("SELECT COUNT(*) FROM spools WHERE qr_code='ODIN-CANDIDATE-SPOOL'").fetchone()[0]),
        "models": int(connection.execute("SELECT COUNT(*) FROM models WHERE name=?", (MODEL_NAME,)).fetchone()[0]),
        "products": int(connection.execute("SELECT COUNT(*) FROM products WHERE sku=?", (PRODUCT_SKU,)).fetchone()[0]),
        "product_components": int(connection.execute("SELECT COUNT(*) FROM product_components pc JOIN products p ON p.id=pc.product_id WHERE p.sku=?", (PRODUCT_SKU,)).fetchone()[0]),
        "orders": int(connection.execute("SELECT COUNT(*) FROM orders WHERE order_number=?", (ORDER_NUMBER,)).fetchone()[0]),
        "order_items": int(connection.execute("SELECT COUNT(*) FROM order_items oi JOIN orders o ON o.id=oi.order_id WHERE o.order_number=?", (ORDER_NUMBER,)).fetchone()[0]),
        "jobs": int(connection.execute("SELECT COUNT(*) FROM jobs WHERE notes LIKE ?", (f"{marker}:%",)).fetchone()[0]),
        "alerts": int(connection.execute("SELECT COUNT(*) FROM alerts WHERE title='ODIN Candidate Gate Alert'").fetchone()[0]),
        "vision_detections": int(connection.execute("SELECT COUNT(*) FROM vision_detections WHERE metadata_json=?", (json.dumps({"marker": marker}),)).fetchone()[0]),
    }
    expected = {
        "users": 3, "printers": 1, "filament_slots": 1, "filaments": 1,
        "spools": 1, "models": 1, "products": 1, "product_components": 1,
        "orders": 1, "order_items": 1, "jobs": 2, "alerts": 1,
        "vision_detections": 1,
    }
    if counts != expected:
        raise SeedSafetyError(f"release-gate fixture count mismatch: {counts}")
    broken_links = int(
        connection.execute(
            "SELECT COUNT(*) FROM order_items oi "
            "LEFT JOIN orders o ON o.id=oi.order_id "
            "LEFT JOIN products p ON p.id=oi.product_id "
            "WHERE o.id IS NULL OR p.id IS NULL"
        ).fetchone()[0]
    )
    if broken_links:
        raise SeedSafetyError("release-gate fixture graph contains broken order links")
    return counts


def seed_release_gate(
    *,
    db_path: Path | str,
    run_id: str,
    marker: str,
    admin_email: str,
    admin_password: str,
    operator_email: str,
    operator_password: str,
    viewer_email: str,
    viewer_password: str,
) -> dict[str, int]:
    """Seed and validate one isolated candidate-gate database."""
    path = Path(db_path)
    if not path.is_file():
        raise SeedSafetyError(f"release-gate database does not exist: {path}")
    allowed_users = {admin_email, operator_email, viewer_email}
    if allowed_users != {ADMIN_EMAIL, OPERATOR_EMAIL, VIEWER_EMAIL}:
        raise SeedSafetyError("release-gate persona identifiers are fixed")
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        _validate_target(connection, run_id, marker, allowed_users)
        with connection:
            user_ids = _ensure_personas(
                connection,
                admin_email,
                admin_password,
                operator_email,
                operator_password,
                viewer_email,
                viewer_password,
            )
            _ensure_domain_graph(connection, run_id, user_ids)
            counts = _manifest(connection, run_id)
        return counts
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed a disposable ODIN candidate gate")
    parser.add_argument("--db-path", default="/data/odin.db")
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    marker = os.environ.get("ODIN_RELEASE_GATE_RUN_ID", "")
    if os.environ.get("ODIN_RELEASE_GATE") != "1":
        raise SeedSafetyError("ODIN_RELEASE_GATE=1 is required")
    passwords = {
        "admin": os.environ.get("ODIN_CANDIDATE_ADMIN_PASSWORD", ""),
        "operator": os.environ.get("ODIN_CANDIDATE_OPERATOR_PASSWORD", ""),
        "viewer": os.environ.get("ODIN_CANDIDATE_VIEWER_PASSWORD", ""),
    }
    if not all(passwords.values()):
        raise SeedSafetyError("all candidate persona passwords are required")
    counts = seed_release_gate(
        db_path=args.db_path,
        run_id=args.run_id,
        marker=marker,
        admin_email=ADMIN_EMAIL,
        admin_password=passwords["admin"],
        operator_email=OPERATOR_EMAIL,
        operator_password=passwords["operator"],
        viewer_email=VIEWER_EMAIL,
        viewer_password=passwords["viewer"],
    )
    print(json.dumps({"run_id": args.run_id, "counts": counts}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
