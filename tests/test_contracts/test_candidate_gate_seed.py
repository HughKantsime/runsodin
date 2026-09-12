from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from backend.scripts.demo_seed_edu import Persona, upsert_personas
from backend.scripts.seed_release_gate import SeedSafetyError, seed_release_gate
from core.base import Base
from core.db import run_core_migrations, run_module_migrations


def _initialized_database(tmp_path: Path) -> Path:
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
        [Persona("candidate-admin@example.invalid", "AdminPass-12345!", "admin", no_mfa=True)],
    )
    return database


def _seed(database: Path, marker: str = "contract-seed-1") -> dict[str, int]:
    return seed_release_gate(
        db_path=database,
        run_id="contract-seed-1",
        marker=marker,
        admin_email="candidate-admin@example.invalid",
        admin_password="AdminPass-12345!",
        operator_email="candidate-operator@example.invalid",
        operator_password="OperatorPass-12345!",
        viewer_email="candidate-viewer@example.invalid",
        viewer_password="ViewerPass-12345!",
    )


def test_release_seed_requires_exact_caller_marker(tmp_path: Path):
    database = _initialized_database(tmp_path)
    with pytest.raises(SeedSafetyError):
        _seed(database, marker="wrong-run")


def test_release_seed_rejects_unrelated_users(tmp_path: Path):
    database = _initialized_database(tmp_path)
    upsert_personas(
        str(database),
        [Persona("unrelated@example.invalid", "UnrelatedPass-12345!", "viewer")],
    )
    with pytest.raises(SeedSafetyError, match="unexpected users"):
        _seed(database)


def test_release_seed_is_idempotent_coherent_and_inert(tmp_path: Path):
    database = _initialized_database(tmp_path)
    first = _seed(database)
    second = _seed(database)
    assert second == first
    assert first == {
        "users": 3,
        "printers": 1,
        "filament_slots": 1,
        "filaments": 1,
        "spools": 1,
        "models": 1,
        "products": 1,
        "product_components": 1,
        "orders": 1,
        "order_items": 1,
        "jobs": 2,
        "alerts": 1,
        "vision_detections": 1,
    }

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        printer = connection.execute(
            "SELECT is_active, api_host, api_key, camera_url, plug_host, plug_auth_token, "
            "timelapse_enabled "
            "FROM printers WHERE name = ?",
            ("ODIN Candidate Gate Printer",),
        ).fetchone()
        assert dict(printer) == {
            "is_active": 0,
            "api_host": None,
            "api_key": None,
            "camera_url": None,
            "plug_host": None,
            "plug_auth_token": None,
            "timelapse_enabled": 0,
        }
        marker = connection.execute(
            "SELECT value FROM system_config WHERE key = 'release_gate_run_id'"
        ).fetchone()[0]
        assert marker == '"contract-seed-1"'
        broken_links = connection.execute(
            "SELECT COUNT(*) FROM order_items oi "
            "LEFT JOIN orders o ON o.id = oi.order_id "
            "LEFT JOIN products p ON p.id = oi.product_id "
            "WHERE o.id IS NULL OR p.id IS NULL"
        ).fetchone()[0]
        assert broken_links == 0
        job_flags = connection.execute(
            "SELECT hold, is_locked FROM jobs ORDER BY id"
        ).fetchall()
        assert [(row[0], row[1]) for row in job_flags] == [(0, 0), (0, 0)]
