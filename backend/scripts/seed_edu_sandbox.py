"""Idempotently seed one fictional, isolated ODIN Education sandbox graph."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from pathlib import Path

ADMIN_EMAIL = "administrator@northstar-lab.example.invalid"
TEACHER_EMAIL = "teacher@northstar-lab.example.invalid"
STUDENT_EMAIL = "student@northstar-lab.example.invalid"
ORGANIZATION_NAME = "Northstar Technical Academy — Additive Lab"
MARKER_KEY = "edu_sandbox_id"
SANDBOX_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,47}$")

PRINTERS = (
    ("Northstar Bambu Replay", "Bambu Lab X1 Carbon", "bambu", True, None, None, ["education", "simulated", "replay", "no-transport", "bambu"]),
    ("Northstar Elegoo Inert", "Elegoo Neptune 4", "elegoo", False, None, None, ["education", "simulated", "inert", "no-transport", "elegoo"]),
    ("Northstar Moonraker Inert", "Klipper / Moonraker", "moonraker", False, None, None, ["education", "simulated", "inert", "no-transport", "moonraker"]),
    ("Northstar PrusaLink Inert", "Prusa MK4", "prusalink", False, None, None, ["education", "simulated", "inert", "no-transport", "prusalink"]),
)

_POPULATION_COUNTS = (
    ("groups", "SELECT COUNT(*) FROM groups"),
    ("printers", "SELECT COUNT(*) FROM printers"),
    ("filament_library", "SELECT COUNT(*) FROM filament_library"),
    ("filament_slots", "SELECT COUNT(*) FROM filament_slots"),
    ("spools", "SELECT COUNT(*) FROM spools"),
    ("models", "SELECT COUNT(*) FROM models"),
    ("products", "SELECT COUNT(*) FROM products"),
    ("product_components", "SELECT COUNT(*) FROM product_components"),
    ("orders", "SELECT COUNT(*) FROM orders"),
    ("order_items", "SELECT COUNT(*) FROM order_items"),
    ("jobs", "SELECT COUNT(*) FROM jobs"),
    ("alerts", "SELECT COUNT(*) FROM alerts"),
    ("quota_usage", "SELECT COUNT(*) FROM quota_usage"),
)


class EduSeedError(RuntimeError):
    """The database is not a safe target for the deterministic EDU graph."""


def _insert_id(connection, statement: str, values: tuple[object, ...]) -> int:
    if getattr(connection, "_postgres", False):
        # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- callers pass only fixed fixture statements and all values remain bound
        row = connection.execute(statement.rstrip().rstrip(";") + " RETURNING id", values).fetchone()
        if row is None:
            raise EduSeedError("insert did not return an identity")
        return int(row[0])
    cursor = connection.execute(statement, values)
    return int(cursor.lastrowid)


def _one_id(connection, statement: str, values: tuple[object, ...]) -> int | None:
    rows = connection.execute(statement, values).fetchall()
    if len(rows) > 1:
        raise EduSeedError("conflicting EDU sandbox fixture identity")
    return int(rows[0][0]) if rows else None


def _marker(connection) -> str | None:
    row = connection.execute("SELECT value FROM system_config WHERE key=?", (MARKER_KEY,)).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row[0])
    except (TypeError, json.JSONDecodeError) as exc:
        raise EduSeedError("EDU sandbox marker is malformed") from exc
    return value if isinstance(value, str) else None


def _validate_target(connection, sandbox_id: str) -> None:
    if not SANDBOX_ID.fullmatch(sandbox_id):
        raise EduSeedError("exact valid sandbox ID is required")
    users = {
        str(row[0]): str(row[1])
        for row in connection.execute("SELECT username, role FROM users").fetchall()
    }
    expected = {ADMIN_EMAIL: "admin", TEACHER_EMAIL: "operator", STUDENT_EMAIL: "viewer"}
    if users != expected:
        raise EduSeedError("sandbox personas must be the exact fixed Northstar accounts")
    existing = _marker(connection)
    if existing is not None and existing != sandbox_id:
        raise EduSeedError("database belongs to a different EDU sandbox")
    if existing is None:
        populated = []
        for table, statement in _POPULATION_COUNTS:
            if int(connection.execute(statement).fetchone()[0]):
                populated.append(table)
        if populated:
            raise EduSeedError("unmarked database contains domain data: " + ", ".join(populated))


def seed_connection(connection, sandbox_id: str) -> dict[str, object]:
    _validate_target(connection, sandbox_id)
    user_rows = connection.execute("SELECT id, username FROM users").fetchall()
    users = {str(row[1]): int(row[0]) for row in user_rows}

    org_id = _one_id(connection, "SELECT id FROM groups WHERE name=?", (ORGANIZATION_NAME,))
    settings = json.dumps({"education": True, "require_job_approval": True}, sort_keys=True)
    if org_id is None:
        org_id = _insert_id(
            connection,
            "INSERT INTO groups (name, description, owner_id, is_org, settings_json) VALUES (?, ?, ?, TRUE, ?)",
            (ORGANIZATION_NAME, "Fictional additive-manufacturing teaching lab", users[ADMIN_EMAIL], settings),
        )
    else:
        connection.execute(
            "UPDATE groups SET description=?, owner_id=?, is_org=TRUE, settings_json=? WHERE id=?",
            ("Fictional additive-manufacturing teaching lab", users[ADMIN_EMAIL], settings, org_id),
        )

    quotas = {
        ADMIN_EMAIL: (None, None, None),
        TEACHER_EMAIL: (5000.0, 100.0, 100),
        STUDENT_EMAIL: (500.0, 12.0, 8),
    }
    for email, (grams, hours, jobs) in quotas.items():
        connection.execute(
            "UPDATE users SET group_id=?, quota_grams=?, quota_hours=?, quota_jobs=?, quota_period='monthly' WHERE id=?",
            (org_id, grams, hours, jobs, users[email]),
        )

    printer_ids: dict[str, int] = {}
    for order, (name, model, api_type, active, api_host, api_key, tags) in enumerate(PRINTERS):
        printer_id = _one_id(connection, "SELECT id FROM printers WHERE name=?", (name,))
        values = (
            name, name.removeprefix("Northstar "), model, 4 if api_type == "bambu" else 1,
            active, False, order, api_type, api_host, api_key, None, False, False,
            json.dumps(tags, sort_keys=True), org_id, False,
        )
        if printer_id is None:
            printer_id = _insert_id(
                connection,
                "INSERT INTO printers (name, nickname, model, slot_count, is_active, camera_enabled, display_order, "
                "api_type, api_host, api_key, camera_url, camera_discovered, timelapse_enabled, tags, org_id, shared) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
        else:
            connection.execute(
                "UPDATE printers SET nickname=?, model=?, slot_count=?, is_active=?, camera_enabled=?, display_order=?, "
                "api_type=?, api_host=?, api_key=?, camera_url=?, camera_discovered=?, timelapse_enabled=?, tags=?, org_id=?, shared=? WHERE id=?",
                values[1:] + (printer_id,),
            )
        printer_ids[api_type] = printer_id

    filament_id = _one_id(
        connection,
        "SELECT id FROM filament_library WHERE brand=? AND name=?",
        ("Northstar EDU", "Classroom PLA"),
    )
    if filament_id is None:
        filament_id = _insert_id(
            connection,
            "INSERT INTO filament_library (brand, name, material, color_hex, cost_per_gram, is_custom) VALUES (?, ?, 'PLA', '2563EB', 0.024, TRUE)",
            ("Northstar EDU", "Classroom PLA"),
        )
    spool_id = _one_id(connection, "SELECT id FROM spools WHERE qr_code=?", ("NORTHSTAR-EDU-SPOOL-001",))
    if spool_id is None:
        spool_id = _insert_id(
            connection,
            "INSERT INTO spools (filament_id, qr_code, color_hex, initial_weight_g, remaining_weight_g, spool_weight_g, price, vendor, status, location_printer_id, location_slot, storage_location, notes, org_id) "
            "VALUES (?, ?, '2563EB', 1000, 820, 250, 24, 'Northstar EDU', 'active', ?, 1, 'Materials Cabinet A', ?, ?)",
            (filament_id, "NORTHSTAR-EDU-SPOOL-001", printer_ids["bambu"], f"edu-sandbox:{sandbox_id}", org_id),
        )
    slot_id = _one_id(
        connection,
        "SELECT id FROM filament_slots WHERE printer_id=? AND slot_number=1",
        (printer_ids["bambu"],),
    )
    if slot_id is None:
        connection.execute(
            "INSERT INTO filament_slots (printer_id, slot_number, filament_type, color, color_hex, assigned_spool_id, spool_confirmed) VALUES (?, 1, 'PLA', 'blue', '#2563EB', ?, TRUE)",
            (printer_ids["bambu"], spool_id),
        )
    else:
        connection.execute(
            "UPDATE filament_slots SET filament_type='PLA', color='blue', color_hex='#2563EB', assigned_spool_id=?, spool_confirmed=TRUE WHERE id=?",
            (spool_id, slot_id),
        )

    model_id = _one_id(connection, "SELECT id FROM models WHERE name=?", ("Northstar Calibration Badge",))
    if model_id is None:
        model_id = _insert_id(
            connection,
            "INSERT INTO models (name, build_time_hours, default_filament_type, color_requirements, category, notes, cost_per_item, units_per_bed, quantity_per_bed, markup_percent, org_id) "
            "VALUES (?, 0.75, 'PLA', ?, 'Classroom', ?, 0.58, 1, 1, 0, ?)",
            ("Northstar Calibration Badge", json.dumps({"slot_1": {"color": "blue", "grams": 22}}), f"edu-sandbox:{sandbox_id}", org_id),
        )
    product_id = _one_id(connection, "SELECT id FROM products WHERE sku=?", ("NORTHSTAR-EDU-001",))
    if product_id is None:
        product_id = _insert_id(
            connection,
            "INSERT INTO products (name, sku, price, description, org_id) VALUES (?, ?, 0, ?, ?)",
            ("Northstar Calibration Lesson", "NORTHSTAR-EDU-001", "Fictional classroom project", org_id),
        )
    if _one_id(connection, "SELECT id FROM product_components WHERE product_id=? AND model_id=?", (product_id, model_id)) is None:
        connection.execute(
            "INSERT INTO product_components (product_id, model_id, quantity_needed, notes) VALUES (?, ?, 1, ?)",
            (product_id, model_id, f"edu-sandbox:{sandbox_id}"),
        )
    order_id = _one_id(connection, "SELECT id FROM orders WHERE order_number=?", ("NORTHSTAR-CLASS-001",))
    if order_id is None:
        order_id = _insert_id(
            connection,
            "INSERT INTO orders (order_number, platform, customer_name, customer_email, status, revenue, notes, org_id) VALUES (?, 'education', 'Northstar Student', ?, 'pending', 0, ?, ?)",
            ("NORTHSTAR-CLASS-001", STUDENT_EMAIL, f"edu-sandbox:{sandbox_id}", org_id),
        )
    order_item_id = _one_id(connection, "SELECT id FROM order_items WHERE order_id=? AND product_id=?", (order_id, product_id))
    if order_item_id is None:
        order_item_id = _insert_id(
            connection,
            "INSERT INTO order_items (order_id, product_id, quantity, unit_price, fulfilled_quantity) VALUES (?, ?, 2, 0, 1)",
            (order_id, product_id),
        )
    for suffix, status, printer_id, submitted, approved in (
        ("pending", "submitted", None, users[STUDENT_EMAIL], None),
        ("completed", "completed", printer_ids["bambu"], users[STUDENT_EMAIL], users[TEACHER_EMAIL]),
    ):
        note = f"edu-sandbox:{sandbox_id}:{suffix}"
        if _one_id(connection, "SELECT id FROM jobs WHERE notes=?", (note,)) is None:
            connection.execute(
                "INSERT INTO jobs (model_id, item_name, quantity, status, priority, printer_id, duration_hours, colors_required, filament_type, notes, estimated_cost, suggested_price, order_item_id, quantity_on_bed, submitted_by, approved_by, charged_to_user_id, charged_to_org_id, required_tags, target_type, queue_position, hold, is_locked) "
                "VALUES (?, ?, 1, ?, 3, ?, 0.75, 'blue', 'PLA', ?, 0.58, 0, ?, 1, ?, ?, ?, ?, '[]', 'specific', ?, FALSE, FALSE)",
                (model_id, f"Northstar Badge ({suffix})", status, printer_id, note, order_item_id, submitted, approved, submitted, org_id, 1 if status == "submitted" else None),
            )
    if _one_id(connection, "SELECT id FROM alerts WHERE title=?", ("Northstar Inert Printer Reminder",)) is None:
        connection.execute(
            "INSERT INTO alerts (user_id, alert_type, severity, title, message, is_read, is_dismissed, printer_id, metadata_json) VALUES (?, 'maintenance_due', 'info', ?, ?, FALSE, FALSE, ?, ?)",
            (users[TEACHER_EMAIL], "Northstar Inert Printer Reminder", "Synthetic classroom reminder", printer_ids["elegoo"], json.dumps({"sandbox_id": sandbox_id, "simulated": True}, sort_keys=True)),
        )
    connection.execute(
        "INSERT INTO quota_usage (user_id, period_key, grams_used, hours_used, jobs_used) VALUES (?, 'demo-period', 22, 0.75, 1) ON CONFLICT(user_id, period_key) DO UPDATE SET grams_used=excluded.grams_used, hours_used=excluded.hours_used, jobs_used=excluded.jobs_used",
        (users[STUDENT_EMAIL],),
    )
    for key, value in ((MARKER_KEY, json.dumps(sandbox_id)), ("require_job_approval", json.dumps("true"))):
        connection.execute(
            "INSERT INTO system_config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    counts = {
        "organizations": int(connection.execute("SELECT COUNT(*) FROM groups WHERE is_org IS TRUE").fetchone()[0]),
        "users": int(connection.execute("SELECT COUNT(*) FROM users WHERE group_id=?", (org_id,)).fetchone()[0]),
        "printers": int(connection.execute("SELECT COUNT(*) FROM printers WHERE org_id=?", (org_id,)).fetchone()[0]),
        "credential_free_printers": int(connection.execute("SELECT COUNT(*) FROM printers WHERE org_id=? AND api_host IS NULL AND api_key IS NULL", (org_id,)).fetchone()[0]),
        "inert_printers": int(connection.execute("SELECT COUNT(*) FROM printers WHERE org_id=? AND is_active IS FALSE AND api_host IS NULL AND api_key IS NULL", (org_id,)).fetchone()[0]),
        "spools": int(connection.execute("SELECT COUNT(*) FROM spools WHERE org_id=?", (org_id,)).fetchone()[0]),
        "models": int(connection.execute("SELECT COUNT(*) FROM models WHERE org_id=?", (org_id,)).fetchone()[0]),
        "products": int(connection.execute("SELECT COUNT(*) FROM products WHERE org_id=?", (org_id,)).fetchone()[0]),
        "orders": int(connection.execute("SELECT COUNT(*) FROM orders WHERE org_id=?", (org_id,)).fetchone()[0]),
        "jobs": int(connection.execute("SELECT COUNT(*) FROM jobs WHERE charged_to_org_id=?", (org_id,)).fetchone()[0]),
        "quota_usage": int(connection.execute("SELECT COUNT(*) FROM quota_usage WHERE user_id=?", (users[STUDENT_EMAIL],)).fetchone()[0]),
    }
    expected = {"organizations": 1, "users": 3, "printers": 4, "credential_free_printers": 4, "inert_printers": 3, "spools": 1, "models": 1, "products": 1, "orders": 1, "jobs": 2, "quota_usage": 1}
    if counts != expected:
        raise EduSeedError(f"EDU sandbox graph count mismatch: {counts}")
    relationships = {
        "personas_in_organization": counts["users"],
        "printers_in_organization": counts["printers"],
        "bambu_spool_assignments": int(connection.execute(
            "SELECT COUNT(*) FROM spools WHERE org_id=? AND location_printer_id=?",
            (org_id, printer_ids["bambu"]),
        ).fetchone()[0]),
        "product_model_components": int(connection.execute(
            "SELECT COUNT(*) FROM product_components WHERE product_id=? AND model_id=?",
            (product_id, model_id),
        ).fetchone()[0]),
        "order_product_items": int(connection.execute(
            "SELECT COUNT(*) FROM order_items WHERE order_id=? AND product_id=?",
            (order_id, product_id),
        ).fetchone()[0]),
        "jobs_charged_to_organization": counts["jobs"],
        "student_quota_entries": counts["quota_usage"],
    }
    expected_relationships = {
        "personas_in_organization": 3,
        "printers_in_organization": 4,
        "bambu_spool_assignments": 1,
        "product_model_components": 1,
        "order_product_items": 1,
        "jobs_charged_to_organization": 2,
        "student_quota_entries": 1,
    }
    if relationships != expected_relationships:
        raise EduSeedError(f"EDU sandbox graph relationship mismatch: {relationships}")
    return {
        "sandbox_id": sandbox_id,
        "organization_id": org_id,
        "stable_identifiers": {
            "organization": ORGANIZATION_NAME,
            "personas": [ADMIN_EMAIL, TEACHER_EMAIL, STUDENT_EMAIL],
            "printers_by_protocol": {item[2]: item[0] for item in PRINTERS},
            "spool_qr": "NORTHSTAR-EDU-SPOOL-001",
            "model": "Northstar Calibration Badge",
            "product_sku": "NORTHSTAR-EDU-001",
            "order_number": "NORTHSTAR-CLASS-001",
        },
        "relationships": relationships,
        "counts": counts,
    }


def seed_sqlite(path: Path, sandbox_id: str) -> dict[str, object]:
    if not path.is_file():
        raise EduSeedError("sandbox database does not exist")
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        with connection:
            return seed_connection(connection, sandbox_id)
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed an isolated fictional Education sandbox")
    parser.add_argument("--db-path", default="/data/odin.db")
    parser.add_argument("--sandbox-id", default=os.environ.get("ODIN_EDU_SANDBOX_ID", ""))
    args = parser.parse_args(argv)
    if os.environ.get("ODIN_EDU_SANDBOX_SEED") != "1":
        raise EduSeedError("ODIN_EDU_SANDBOX_SEED=1 is required")
    database_url = os.environ.get("DATABASE_URL", "")
    if database_url.startswith(("postgresql://", "postgres://")):
        from core.db_utils import get_db
        with get_db() as connection:
            try:
                manifest = seed_connection(connection, args.sandbox_id)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
    else:
        manifest = seed_sqlite(Path(args.db_path), args.sandbox_id)
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
