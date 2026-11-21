"""The Fjord accessibility fixture matches the responses an admin receives."""
import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from core.base import Base
from core.schema.bootstrap import import_all_models
from modules.inventory.routes.conflicts import conflicts
from modules.inventory.routes.spools import get_spoolman_link_conflicts
from modules.inventory.routes.stock import reorder_rollup


@pytest.fixture(scope="module")
def empty_inventory():
    engine = create_engine("sqlite://")
    import_all_models()
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


@pytest.mark.parametrize(("path", "handler"), [
    ("/api/inventory/conflicts", conflicts),
    ("/api/spools/reorder", reorder_rollup),
    ("/api/spools/spoolman-link-conflicts", get_spoolman_link_conflicts),
])
def test_empty_inventory_fixture_matches_handler(empty_inventory, path, handler):
    fixture = Path(__file__).resolve().parents[1] / "accessibility/fixtures/fjord.json"
    expected = handler(current_user={"id": 1, "role": "admin", "group_id": None}, db=empty_inventory)
    assert json.loads(fixture.read_text())[f"GET {path}"] == expected


def test_notifications_quiet_hours_fixture_matches_handler(empty_inventory):
    from modules.system.routes_config import get_quiet_hours_config
    fixture = Path(__file__).resolve().parents[1] / "accessibility/fixtures/fjord.json"
    expected = asyncio.run(get_quiet_hours_config(db=empty_inventory))
    assert json.loads(fixture.read_text())["GET /api/config/quiet-hours"] == expected
