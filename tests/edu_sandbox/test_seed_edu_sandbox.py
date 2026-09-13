from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from backend.scripts.demo_seed_edu import Persona, upsert_personas
from backend.scripts.seed_edu_sandbox import (
    ADMIN_EMAIL,
    ORGANIZATION_NAME,
    PRINTERS,
    STUDENT_EMAIL,
    TEACHER_EMAIL,
    _POPULATION_COUNTS,
    EduSeedError,
    seed_sqlite,
)
from core.base import Base
from core.db import run_core_migrations, run_module_migrations


def _initialized_database(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    database = tmp_path / "odin.db"
    database_url = f"sqlite:///{database}"
    import core.models  # noqa: F401
    import modules.archives.models  # noqa: F401
    import modules.inventory.models  # noqa: F401
    import modules.jobs.models  # noqa: F401
    import modules.models_library.models  # noqa: F401
    import modules.notifications.models  # noqa: F401
    import modules.orders.models  # noqa: F401
    import modules.printers.models  # noqa: F401
    import modules.system.models  # noqa: F401
    import modules.vision.models  # noqa: F401

    Base.metadata.create_all(create_engine(database_url))
    run_core_migrations(database_url=database_url)
    run_module_migrations(Path(__file__).parents[2] / "backend" / "modules", database_url=database_url)
    upsert_personas(
        str(database),
        [
            Persona(ADMIN_EMAIL, "AdminPass-Aa1!", "admin", no_mfa=True),
            Persona(TEACHER_EMAIL, "TeacherPass-Aa1!", "operator", no_mfa=True),
            Persona(STUDENT_EMAIL, "StudentPass-Aa1!", "viewer", no_mfa=True),
        ],
    )
    return database


def test_seed_creates_one_tenant_and_truthful_protocol_graph(tmp_path: Path):
    database = _initialized_database(tmp_path)
    manifest = seed_sqlite(database, "school-one")
    assert manifest["counts"] == {
        "organizations": 1,
        "users": 3,
        "printers": 4,
        "credential_free_printers": 4,
        "inert_printers": 3,
        "spools": 1,
        "models": 1,
        "products": 1,
        "orders": 1,
        "jobs": 2,
        "quota_usage": 1,
    }
    assert manifest["stable_identifiers"]["product_sku"] == "NORTHSTAR-EDU-001"
    assert manifest["stable_identifiers"]["printers_by_protocol"] == {
        item[2]: item[0] for item in PRINTERS
    }
    assert manifest["relationships"] == {
        "personas_in_organization": 3,
        "printers_in_organization": 4,
        "bambu_spool_assignments": 1,
        "product_model_components": 1,
        "order_product_items": 1,
        "jobs_charged_to_organization": 2,
        "student_quota_entries": 1,
    }

    connection = sqlite3.connect(database)
    org = connection.execute("SELECT id, name, is_org FROM groups").fetchone()
    users = connection.execute(
        "SELECT username, role, group_id, quota_jobs FROM users ORDER BY role"
    ).fetchall()
    printers = connection.execute(
        "SELECT name, api_type, is_active, api_host, api_key, camera_url, tags, org_id FROM printers ORDER BY display_order"
    ).fetchall()
    connection.close()

    assert org[1:] == (ORGANIZATION_NAME, 1)
    assert {row[0]: row[1] for row in users} == {
        ADMIN_EMAIL: "admin",
        TEACHER_EMAIL: "operator",
        STUDENT_EMAIL: "viewer",
    }
    assert all(row[2] == org[0] for row in users)
    assert [row[1] for row in printers] == [item[2] for item in PRINTERS]
    bambu = printers[0]
    assert bambu[2:6] == (1, None, None, None)
    assert "replay" in bambu[6] and "no-transport" in bambu[6]
    for inert in printers[1:]:
        assert inert[2:6] == (0, None, None, None)
        assert "inert" in inert[6] and "no-transport" in inert[6]
    assert all(row[7] == org[0] for row in printers)


def test_seed_is_idempotent_and_second_run_has_same_manifest(tmp_path: Path):
    database = _initialized_database(tmp_path)
    first = seed_sqlite(database, "school-one")
    second = seed_sqlite(database, "school-one")
    assert second == first


def test_seed_rejects_conflicting_or_populated_target(tmp_path: Path):
    database = _initialized_database(tmp_path)
    seed_sqlite(database, "school-one")
    with pytest.raises(EduSeedError, match="different EDU sandbox"):
        seed_sqlite(database, "school-two")

    other = _initialized_database(tmp_path / "other")
    connection = sqlite3.connect(other)
    connection.execute("INSERT INTO groups (name, is_org) VALUES ('Foreign School', TRUE)")
    connection.commit()
    connection.close()
    with pytest.raises(EduSeedError, match="contains domain data"):
        seed_sqlite(other, "school-one")


def test_seed_preflight_inventories_every_written_domain_table() -> None:
    inventoried = {table for table, _statement in _POPULATION_COUNTS}
    assert {
        "groups",
        "printers",
        "filament_library",
        "filament_slots",
        "spools",
        "models",
        "products",
        "product_components",
        "orders",
        "order_items",
        "jobs",
        "alerts",
        "quota_usage",
    } <= inventoried


def test_seed_rejects_existing_quota_before_any_domain_write(tmp_path: Path) -> None:
    database = _initialized_database(tmp_path)
    connection = sqlite3.connect(database)
    student_id = connection.execute(
        "SELECT id FROM users WHERE username=?", (STUDENT_EMAIL,)
    ).fetchone()[0]
    connection.execute(
        "INSERT INTO quota_usage (user_id, period_key, grams_used, hours_used, jobs_used) "
        "VALUES (?, 'foreign-period', 1, 1, 1)",
        (student_id,),
    )
    connection.commit()
    before = {
        table: int(connection.execute(statement).fetchone()[0])
        for table, statement in _POPULATION_COUNTS
    }
    connection.close()

    with pytest.raises(EduSeedError, match="quota_usage"):
        seed_sqlite(database, "school-one")

    connection = sqlite3.connect(database)
    after = {
        table: int(connection.execute(statement).fetchone()[0])
        for table, statement in _POPULATION_COUNTS
    }
    connection.close()
    assert after == before


def test_edu_bambu_replay_uses_internal_synthetic_config_not_printer_credentials(monkeypatch):
    from modules.printers.monitors import mqtt_monitor

    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE printers (id INTEGER PRIMARY KEY, name TEXT, model TEXT, api_type TEXT, "
            "api_host TEXT, api_key TEXT, is_active BOOLEAN)"
        ))
        connection.execute(text(
            "INSERT INTO printers (id, name, api_type, api_host, api_key, is_active) "
            "VALUES (1, 'Northstar Bambu Replay', 'bambu', NULL, NULL, TRUE)"
        ))
    monkeypatch.setattr(mqtt_monitor, "engine", engine)
    monkeypatch.setenv("ENCRYPTION_KEY", "synthetic-test-key")
    monkeypatch.setenv("ODIN_EDU_SANDBOX_SEED", "1")
    monkeypatch.setenv("ODIN_EDU_BAMBU_REPLAY", "1")

    printers = mqtt_monitor.MQTTMonitorDaemon().load_printers()

    assert printers == [{
        "id": 1,
        "name": "Northstar Bambu Replay",
        "ip": "mosquitto",
        "serial": "EDU-BAMBU-SIM-001",
        "access_code": "simulation-only",
    }]
