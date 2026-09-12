"""Stage or verify an exact-image SQLite relational restore."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time

from core.database_config import create_database_engine
from core.schema import schema_fingerprint
from modules.system.backup_service import (
    create_online_backup,
    stage_restore,
    validate_database,
)


DATABASE_URL = os.environ["DATABASE_URL"]
DATABASE_PATH = DATABASE_URL.removeprefix("sqlite:///")


def relationship_graph_fingerprint() -> str:
    connection = sqlite3.connect(DATABASE_PATH)
    try:
        inventory = connection.execute(
            "SELECT p.name, fs.slot_number, s.qr_code, f.brand, "
            "f.material FROM printers p "
            "JOIN filament_slots fs ON fs.printer_id=p.id "
            "JOIN spools s ON s.id=fs.assigned_spool_id "
            "JOIN filament_library f ON f.id=s.filament_id "
            "WHERE p.name='ODIN Candidate Gate Printer' ORDER BY fs.slot_number"
        ).fetchall()
        production = connection.execute(
            "SELECT j.item_name, j.status, m.name, o.order_number, "
            "pc.quantity_needed "
            "FROM jobs j JOIN models m ON m.id=j.model_id "
            "JOIN order_items oi ON oi.id=j.order_item_id "
            "JOIN orders o ON o.id=oi.order_id "
            "JOIN product_components pc ON pc.product_id=oi.product_id "
            "WHERE o.order_number='ODIN-CANDIDATE-ORDER-001' ORDER BY j.item_name"
        ).fetchall()
    finally:
        connection.close()
    payload = json.dumps(
        {"inventory": inventory, "production": production},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def current_schema_fingerprint() -> str:
    engine = create_database_engine(DATABASE_URL, role="api")
    try:
        with engine.connect() as connection:
            return schema_fingerprint(connection)
    finally:
        engine.dispose()


def stage() -> None:
    graph = relationship_graph_fingerprint()
    schema = current_schema_fingerprint()
    started = time.monotonic()
    candidate, metadata = create_online_backup(DATABASE_URL)
    backup_duration = time.monotonic() - started
    validation_started = time.monotonic()
    validate_database(candidate)
    validation_duration = time.monotonic() - validation_started

    connection = sqlite3.connect(DATABASE_PATH)
    try:
        spool_result = connection.execute(
            "UPDATE spools SET qr_code='ODIN-MUTATED-SPOOL' "
            "WHERE qr_code='ODIN-CANDIDATE-SPOOL'"
        )
        job_result = connection.execute(
            "UPDATE jobs SET item_name=item_name || ' mutated' "
            "WHERE item_name LIKE 'ODIN Candidate Cube%'"
        )
        if spool_result.rowcount != 1 or job_result.rowcount != 2:
            raise AssertionError(
                "SQLite relationship mutation did not affect expected rows: "
                f"spools={spool_result.rowcount}, jobs={job_result.rowcount}"
            )
        connection.commit()
    finally:
        connection.close()
    if relationship_graph_fingerprint() == graph:
        raise AssertionError("SQLite relationship mutation did not change its fingerprint")

    stage_restore(candidate, DATABASE_URL)
    evidence = {
        "dialect": "sqlite",
        "backup_size_bytes": metadata["size_bytes"],
        "backup_duration_seconds": round(backup_duration, 3),
        "validation_duration_seconds": round(validation_duration, 3),
        "restore_duration_seconds": 0.0,
        "table_count": metadata["table_count"],
        "toc_entries": 0,
        "toc_fingerprint": None,
        "schema_fingerprint": schema,
        "relationship_graph_fingerprint": graph,
    }
    print("database-parity-evidence: " + json.dumps(evidence, sort_keys=True))


def verify() -> None:
    expected = os.environ["ODIN_EXPECTED_GRAPH_FINGERPRINT"]
    actual = relationship_graph_fingerprint()
    if actual != expected:
        raise AssertionError(
            f"restored SQLite relationship fingerprint mismatch: {actual}"
        )
    print("sqlite-relational-restore: PASS graph-fingerprint")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"stage", "verify"}:
        raise SystemExit("usage: sqlite_restore_drill.py stage|verify")
    stage() if sys.argv[1] == "stage" else verify()
