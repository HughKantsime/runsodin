"""Contract tests for the opt-in read-only external filament display.

Uses the shared contract database fixture; no ODIN_TEST_BACKEND, live printer,
provider or login is required. The display HTTP client is replaced in-process
and every request is refused before outbound I/O.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401

from core.config import settings
from modules.inventory.routes._helpers import ScanAssignRequest
from modules.inventory.routes.filament_slots import (
    assign_spool_to_slot,
    confirm_slot_assignment,
)
from modules.inventory.routes.spool_ops import scan_assign_spool
from modules.inventory.routes.spools import SpoolLoadRequest, load_spool
from modules.printers import ledger_display
from modules.printers.routes_ams import sync_ams_state
from modules.printers.routes_bambu import ManualSlotAssignment, manual_slot_assignment
from modules.printers.routes_crud import (
    get_printer,
    list_filament_slots,
    list_printers,
    update_filament_slot,
    update_printer,
)
from modules.printers.routes_filament_slots import (
    AssignSpoolRequest,
    assign_spool_to_slot as agent_assign,
)
from modules.printers.schemas import FilamentSlotUpdate, PrinterUpdate

BACKEND = Path(__file__).resolve().parents[2] / "backend"


class _FakeStream:
    def __init__(self, client: "FakeClient"):
        self.client = client

    def __enter__(self):
        self.client.entered.set()
        return self

    @property
    def status_code(self):
        return self.client.status

    def __exit__(self, *_args):
        return False

    def iter_raw(self, chunk_size=None):
        for chunk in self.client.chunks:
            if self.client.gate is not None:
                self.client.gate.wait(5)
            if self.client.delay:
                time.sleep(self.client.delay)
            yield chunk


class FakeClient:
    """Callable stand-in for httpx.Client with a streamed response."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.init_kwargs = None
        self.status = 200
        self.chunks = [json.dumps(ledger_payload()).encode()]
        self.error = None
        self.gate = None
        self.delay = 0.0
        self.entered = threading.Event()

    def __call__(self, **kwargs):
        self.init_kwargs = kwargs
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def stream(self, method, url):
        self.calls.append((method, url))
        if self.error is not None:
            raise self.error
        return _FakeStream(self)


def ledger_payload(key="u1", *, tools=None):
    return {"printers": [{
        "key": key,
        "tools": tools if tools is not None else [
            {
                "tool_index": 0,
                "display_name": "Tool 1",
                "spool_id": 8301,
                "spool_status": "mapped",
                "spool": {
                    "id": 8301,
                    "material": "PLA+",
                    "name": "Matte Blue",
                    "brand": "Example Maker",
                    "color_hex": "#1a2B3c",
                    "remaining_weight_g": 150,
                    "initial_weight_g": 100,
                },
                "observed_filament": None,
            },
            {
                "tool_index": 1,
                "display_name": "Tool 2",
                "spool_id": None,
                "spool_status": "unmapped",
                "spool": None,
                "observed_filament": None,
            },
        ],
    }]}


def reset_cache():
    with ledger_display._cache_lock:
        ledger_display._cached_url = None
        ledger_display._cached_until = 0
        ledger_display._cached_printers = None
        ledger_display._cached_available = False
        ledger_display._refreshing_url = None


def wait_for_refresh(timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with ledger_display._cache_lock:
            if ledger_display._refreshing_url is None:
                return
        time.sleep(0.005)
    raise AssertionError("filament ledger refresh did not finish")


def refresh_and_wait():
    """Trigger a background refresh if due and wait for it to publish."""
    ledger_display._fetch_printers()
    wait_for_refresh()
    with ledger_display._cache_lock:
        return ledger_display._cached_available, dict(ledger_display._cached_printers or {})


@pytest.fixture(autouse=True)
def _clean_cache():
    reset_cache()
    yield
    assert ledger_display._transport_lock.acquire(timeout=2)
    ledger_display._transport_lock.release()
    reset_cache()


@pytest.fixture
def db(review_db):
    review_db.execute(text(
        "UPDATE printers SET slot_count=4, shared=0, tags='[]', timelapse_enabled=0 "
        "WHERE id IN (9,10)"
    ))
    review_db.execute(text(
        "UPDATE filament_slots SET filament_type='PLA', color='old local value', "
        "color_hex='#000000', spoolman_spool_id=7654, assigned_spool_id=NULL, "
        "spool_confirmed=0 WHERE printer_id=9"
    ))
    review_db.execute(text(
        "UPDATE filament_slots SET filament_type='PETG', color='local value', "
        "color_hex='#ffffff', spoolman_spool_id=NULL WHERE printer_id=10"
    ))
    review_db.execute(text(
        "INSERT INTO filament_library(id,brand,name,material,color_hex) "
        "VALUES (3,'Local','Test','PLA','#112233')"
    ))
    review_db.execute(text(
        "INSERT INTO spools(id,filament_id,qr_code,initial_weight_g,remaining_weight_g,"
        "spool_weight_g,status,org_id) VALUES (3,3,'synthetic-qr',1000,900,250,'active',1)"
    ))
    review_db.commit()
    return review_db


@pytest.fixture
def fake(db, monkeypatch):
    monkeypatch.setattr(settings, "filament_ledger_url", "http://127.0.0.1:9876")
    monkeypatch.setattr(settings, "filament_ledger_printers", {9: "u1"})
    client = FakeClient()
    monkeypatch.setattr(ledger_display.httpx, "Client", client)
    monkeypatch.setattr(
        ledger_display,
        "pinned_device_destination",
        lambda _endpoint: nullcontext(["192.168.1.42"]),
    )
    return client


def db_snapshot(db):
    slots = tuple(db.execute(text(
        "SELECT printer_id,slot_number,filament_type,color,color_hex,spoolman_spool_id,"
        "assigned_spool_id,spool_confirmed FROM filament_slots ORDER BY printer_id,slot_number"
    )).all())
    spools = tuple(db.execute(text(
        "SELECT id,remaining_weight_g,location_printer_id,location_slot FROM spools ORDER BY id"
    )).all())
    return slots, spools


def test_unconfigured_rows_are_same_orm_and_do_not_fetch(db, fake):
    from modules.printers.models import Printer

    row = db.query(Printer).filter_by(id=10).one()
    result = ledger_display.enrich_printers([row])
    assert result[0] is row
    assert fake.calls == []


def test_configured_enrichment_maps_zero_based_tools_and_never_writes(db, fake):
    from modules.printers.models import Printer

    before = db_snapshot(db)
    refresh_and_wait()
    rows = db.query(Printer).order_by("id").all()
    result = ledger_display.enrich_printers(rows)
    assert result[0].filament_source == "filament-ledger"
    assert result[0].loaded_colors == []
    assert result[0].filament_source_status == "partial"
    assert result[0].filament_slots[0].slot_number == 1
    assert result[0].filament_slots[0].filament_type.value == "PLA"
    assert result[0].filament_slots[0].material_type == "PLA+"
    assert result[0].filament_slots[0].color == "Example Maker Matte Blue"
    assert result[0].filament_slots[0].display_name == "Tool 1"
    assert result[0].filament_slots[0].color_hex == "1A2B3C"
    assert result[0].filament_slots[0].remaining == 100.0
    assert result[0].filament_slots[0].external_spool_id == 8301
    assert result[0].filament_slots[0].spoolman_spool_id is None
    assert result[0].filament_slots[0].assigned_spool_id is None
    assert result[0].filament_slots[1].mapping_status == "unmapped"
    assert result[0].filament_slots[1].filament_type.value == "Unknown"
    assert result[1] is rows[1]
    assert fake.calls == [("GET", "http://127.0.0.1:9876/api/filament-display")]
    assert fake.init_kwargs["follow_redirects"] is False
    assert fake.init_kwargs["trust_env"] is False
    assert isinstance(fake.init_kwargs["timeout"], ledger_display.httpx.Timeout)
    assert db_snapshot(db) == before


def test_unmapped_tools_remain_explicit_without_inventing_material(db, fake):
    from modules.printers.models import Printer

    fake.chunks = [json.dumps(ledger_payload(tools=[{
        "tool_index": 0, "display_name": "Tool 1", "spool_id": None,
        "spool_status": "unmapped", "spool": None, "observed_filament": None,
    }])).encode()]
    refresh_and_wait()
    row = db.query(Printer).filter_by(id=9).one()
    result = ledger_display.enrich_printers([row])[0]
    assert result.filament_source_status == "partial"
    assert result.filament_slots[0].mapping_status == "unmapped"
    assert result.filament_slots[0].filament_type.value == "Unknown"
    assert result.filament_slots[0].color is None
    assert result.filament_slots[0].remaining is None


def test_empty_tools_and_missing_printer_are_unavailable(db, fake):
    from modules.printers.models import Printer

    for payload in (ledger_payload(tools=[]), {"printers": []}):
        reset_cache()
        fake.chunks = [json.dumps(payload).encode()]
        refresh_and_wait()
        row = db.query(Printer).filter_by(id=9).one()
        result = ledger_display.enrich_printers([row])[0]
        assert result.filament_source_status == "unavailable"
        assert result.filament_slots == []


def test_provider_failure_and_bad_payload_back_off_and_hide_stale_local_slots(db, fake):
    from modules.printers.models import Printer

    before = db_snapshot(db)
    for response in (
        b'{"bad": []}',
        b'{"printers": "bad"}',
        json.dumps({}).encode(),
    ):
        reset_cache()
        fake.calls = []
        fake.chunks = [response]
        fake.status = 503 if response == json.dumps({}).encode() else 200
        refresh_and_wait()
        row = db.query(Printer).filter_by(id=9).one()
        result1 = ledger_display.enrich_printers([row])[0]
        result2 = ledger_display.enrich_printers([row])[0]
        assert result1.filament_source_status == "unavailable"
        assert result1.filament_slots == []
        assert result1.loaded_colors == []
        assert result2.filament_slots == []
        assert len(fake.calls) == 1
    assert db_snapshot(db) == before


def test_malformed_mapped_details_fail_closed_without_raising(db, fake):
    from modules.printers.models import Printer

    fake.chunks = [json.dumps(ledger_payload(tools=[{
        "tool_index": 0, "display_name": "Tool 1", "spool_id": 5,
        "spool_status": "mapped", "spool": {"id": 5, "color_hex": "#nothex"},
    }])).encode()]
    refresh_and_wait()
    row = db.query(Printer).filter_by(id=9).one()
    result = ledger_display.enrich_printers([row])[0]
    assert result.filament_source_status == "unavailable"
    assert result.filament_slots == []


def test_read_routes_enrich_after_org_filter_and_keep_local_rows(db, fake):
    user = {"role": "viewer", "group_id": 1}
    before = db_snapshot(db)
    refresh_and_wait()
    results = list_printers(False, None, None, user, {}, db)
    assert [r.id for r in results] == [9, 10]
    assert results[0].filament_source == "filament-ledger"
    assert results[1] is not None
    with pytest.raises(HTTPException) as caught:
        get_printer(10, current_user={"role": "viewer", "group_id": 2}, _agent_scope={}, db=db)
    assert caught.value.status_code == 404
    with pytest.raises(HTTPException) as caught:
        list_filament_slots(9, {"role": "viewer", "group_id": 2}, db)
    assert caught.value.status_code == 404
    slots = list_filament_slots(9, user, db)
    assert slots[0].display_name == "Tool 1"
    assert len(fake.calls) == 1
    assert db_snapshot(db) == before


def test_shared_printer_read_visibility_survives_enrichment(db, fake):
    db.execute(text("UPDATE printers SET shared=1 WHERE id IN (9,10)"))
    db.commit()
    before = db_snapshot(db)
    refresh_and_wait()
    user = {"role": "viewer", "group_id": 2}
    rows = list_printers(False, None, None, user, {}, db)
    assert [row.id for row in rows] == [9, 10]
    assert get_printer(
        9, current_user=user, _agent_scope={}, db=db
    ).filament_source == "filament-ledger"
    assert list_filament_slots(9, user, db)[0].external_spool_id == 8301
    assert db_snapshot(db) == before


def test_local_filament_mutation_routes_reject_configured_printer(db, fake):
    user = {"role": "admin", "group_id": None}
    for action in (
        lambda: update_filament_slot(9, 1, FilamentSlotUpdate(color="changed"), user, db),
        lambda: sync_ams_state(9, user, db),
        lambda: assign_spool_to_slot(9, 1, 3, user, db),
        lambda: confirm_slot_assignment(9, 1, user, db),
        lambda: scan_assign_spool(
            ScanAssignRequest(qr_code="synthetic-qr", printer_id=9, slot=1), user, db
        ),
        lambda: load_spool(3, SpoolLoadRequest(printer_id=9, slot_number=1), user, db),
        lambda: agent_assign(
            AssignSpoolRequest(spool_id=3, printer_id=9, ams_slot=1), None, user, {}, db
        ),
        lambda: asyncio.run(
            manual_slot_assignment(9, 1, ManualSlotAssignment(color="changed"), user, db)
        ),
        lambda: update_printer(9, PrinterUpdate(slot_count=5), user, db),
    ):
        with pytest.raises(HTTPException) as err:
            action()
        assert err.value.status_code == 409
    db.rollback()


def test_local_mutations_remain_available_for_unconfigured_printer(db, fake):
    user = {"role": "admin", "group_id": None}
    result = update_filament_slot(10, 1, FilamentSlotUpdate(color="edited"), user, db)
    assert result.color == "edited"


def test_alternate_bambu_mutations_reject_before_hardware_calls(db, fake):
    from modules.printers import routes_ams_env, routes_bambu, route_utils

    before = db_snapshot(db)
    user = {"role": "admin", "group_id": None}
    with patch.object(routes_bambu, "BAMBU_AVAILABLE", True), \
            patch.object(routes_bambu, "sync_ams_filaments") as sync, \
            patch.object(route_utils, "_bambu_command_direct") as command:
        with pytest.raises(HTTPException) as err:
            asyncio.run(routes_bambu.sync_bambu_ams(9, user, db))
        assert err.value.status_code == 409
        for spool_id in (None, 3):
            with pytest.raises(HTTPException) as err:
                asyncio.run(routes_ams_env.configure_ams_slot(
                    9, 0, 0,
                    routes_ams_env.AMSSlotConfigRequest(
                        material="PLA", color="#123456", spool_id=spool_id
                    ),
                    user,
                    db,
                ))
            assert err.value.status_code == 409
        sync.assert_not_called()
        command.assert_not_called()
    assert db_snapshot(db) == before


def test_cross_org_mutations_do_not_disclose_binding(db, fake):
    user = {"role": "operator", "group_id": 2}
    for action in (
        lambda: assign_spool_to_slot(9, 1, 3, user, db),
        lambda: confirm_slot_assignment(9, 1, user, db),
        lambda: manual_slot_assignment(9, 1, ManualSlotAssignment(color="changed"), user, db),
    ):
        with pytest.raises(HTTPException) as err:
            result = action()
            if asyncio.iscoroutine(result):
                asyncio.run(result)
        assert err.value.status_code == 404
    assert fake.calls == []


def test_spool_moves_and_unloads_cannot_clear_linked_source(db, fake):
    from modules.inventory.models import Spool
    from modules.inventory.slot_assignments import assign_slot, unload_spool_graph
    from modules.printers.models import FilamentSlot

    db.execute(text("UPDATE filament_slots SET assigned_spool_id=3 WHERE printer_id=9"))
    db.execute(text("UPDATE spools SET location_printer_id=9,location_slot=1 WHERE id=3"))
    db.commit()
    before = db_snapshot(db)
    user = {"role": "admin", "group_id": None}
    destination = db.query(FilamentSlot).filter_by(printer_id=10).one()
    with pytest.raises(HTTPException) as err:
        assign_slot(db, destination, 3, user)
    assert err.value.status_code == 409
    db.rollback()
    spool = db.query(Spool).filter_by(id=3).one()
    with pytest.raises(HTTPException) as err:
        unload_spool_graph(db, spool, user)
    assert err.value.status_code == 409
    db.rollback()
    assert db_snapshot(db) == before


def test_destination_policy_blocks_before_http_even_outside_itar(db, fake, monkeypatch):
    from core.webhook_utils import pinned_device_destination
    from modules.printers.models import Printer

    monkeypatch.setattr(settings, "filament_ledger_url", "http://127.0.0.1:9876")
    monkeypatch.setattr(settings, "filament_ledger_printers", {9: "u1"})
    # Use the real pinned-destination policy (no test override).
    monkeypatch.setattr(ledger_display, "pinned_device_destination", pinned_device_destination)
    refresh_and_wait()
    row = db.query(Printer).filter_by(id=9).one()
    result = ledger_display.enrich_printers([row])[0]
    assert result.filament_source_status == "unavailable"
    assert fake.calls == []
    assert ledger_display._cached_available is False


def test_binding_environment_accepts_blank_and_valid_json_only(monkeypatch):
    from core.config import Settings

    for raw in ("", "  ", "{}", "null"):
        with patch.dict("os.environ", {"FILAMENT_LEDGER_PRINTERS": raw}):
            assert Settings(_env_file=None).filament_ledger_printers == {}
    with patch.dict("os.environ", {"FILAMENT_LEDGER_PRINTERS": '{"7":"u1"}'}):
        assert Settings(_env_file=None).filament_ledger_printers == {7: "u1"}
    for raw in ("not-json", "[]", '{"bad-id":"u1"}'):
        with patch.dict("os.environ", {"FILAMENT_LEDGER_PRINTERS": raw}):
            with pytest.raises(ValueError):
                Settings(_env_file=None)


def test_public_or_mixed_pinned_addresses_fail_before_http(db, fake, monkeypatch):
    from modules.printers.models import Printer

    row = db.query(Printer).filter_by(id=9).one()
    for ips in (["8.8.8.8"], ["192.168.1.42", "8.8.8.8"], []):
        reset_cache()
        fake.calls = []
        monkeypatch.setattr(
            ledger_display, "pinned_device_destination", lambda _url, value=ips: nullcontext(value)
        )
        assert refresh_and_wait() == (False, {})
        assert fake.calls == []
        assert row is not None


def test_no_binding_disables_provider_calls_and_guard(db, fake, monkeypatch):
    monkeypatch.setattr(settings, "filament_ledger_printers", {})
    from modules.printers.models import Printer

    row = db.query(Printer).filter_by(id=9).one()
    assert ledger_display.enrich_printers([row])[0] is row
    result = update_filament_slot(
        9, 1, FilamentSlotUpdate(color="ok"), {"role": "admin", "group_id": None}, db
    )
    assert result.color == "ok"
    assert fake.calls == []


def test_inventory_writers_use_the_core_interface_not_the_display_adapter():
    inventory = BACKEND / "modules/inventory"
    sources = {
        "routes/filament_slots.py": inventory / "routes/filament_slots.py",
        "routes/spool_ops.py": inventory / "routes/spool_ops.py",
        "routes/spools.py": inventory / "routes/spools.py",
        "slot_assignments.py": inventory / "slot_assignments.py",
    }
    for name, path in sources.items():
        assert "modules.printers.ledger_display" not in path.read_text(encoding="utf-8"), name

    from core.interfaces.filament_ownership import (
        FilamentOwnershipProvider,
        ensure_local_filament_management,
    )
    from core.registry import registry

    provider = registry.get_provider("FilamentOwnershipProvider")
    assert isinstance(provider, FilamentOwnershipProvider)

    # The registered provider refuses a bound printer and permits an unbound one.
    settings.filament_ledger_printers = {9: "u1"}
    try:
        with pytest.raises(HTTPException) as err:
            ensure_local_filament_management(9)
        assert err.value.status_code == 409
        ensure_local_filament_management(10)
    finally:
        settings.filament_ledger_printers = {}


def test_interface_fails_closed_from_configuration_without_a_provider(monkeypatch):
    from core.interfaces.filament_ownership import ensure_local_filament_management
    from core.registry import registry

    monkeypatch.setattr(settings, "filament_ledger_printers", {9: "u1"})
    monkeypatch.delitem(registry._providers, "FilamentOwnershipProvider", raising=False)
    with pytest.raises(HTTPException) as err:
        ensure_local_filament_management(9)
    assert err.value.status_code == 409
    ensure_local_filament_management(10)


def test_concurrent_readers_are_served_stale_and_only_one_refresh_runs(db, fake):
    # Prime a successful snapshot, then let it expire.
    assert refresh_and_wait()[0] is True
    assert len(fake.calls) == 1
    with ledger_display._cache_lock:
        ledger_display._cached_until = 0

    fake.gate = threading.Event()
    fake.entered.clear()
    started = time.monotonic()
    available, printers = ledger_display._fetch_printers()
    assert time.monotonic() - started < 0.5
    assert available is True and "u1" in printers
    assert fake.entered.wait(2)

    # A second reader during the refresh is also served the stale snapshot.
    started = time.monotonic()
    assert ledger_display._fetch_printers()[1] == printers
    assert time.monotonic() - started < 0.5
    assert len(fake.calls) == 2  # the initial read and the single in-flight refresh
    fake.gate.set()
    wait_for_refresh()
    assert len(fake.calls) == 2


def test_concurrent_reader_without_a_snapshot_is_unavailable_immediately(db, fake):
    fake.gate = threading.Event()
    fake.entered.clear()
    started = time.monotonic()
    assert ledger_display._fetch_printers() == (False, {})
    assert time.monotonic() - started < 0.5
    assert fake.entered.wait(2)
    started = time.monotonic()
    assert ledger_display._fetch_printers() == (False, {})
    assert time.monotonic() - started < 0.5
    assert len(fake.calls) == 1
    fake.gate.set()
    wait_for_refresh()
    assert len(fake.calls) == 1


def test_whole_refresh_deadline_covers_dns_resolution(db, fake, monkeypatch):
    monkeypatch.setattr(ledger_display, "_REQUEST_TIMEOUT", 0.2)
    gate = threading.Event()

    @contextmanager
    def slow_pin(_endpoint):
        gate.wait(5)
        yield ["192.168.1.42"]

    monkeypatch.setattr(ledger_display, "pinned_device_destination", slow_pin)
    started = time.monotonic()
    try:
        assert ledger_display._fetch_printers() == (False, {})
        wait_for_refresh()
        assert time.monotonic() - started < 1.0
        assert fake.calls == []
        assert ledger_display._cached_available is False
    finally:
        gate.set()
    # A late worker result is abandoned rather than published.
    time.sleep(0.1)
    assert ledger_display._cached_available is False


def test_whole_refresh_deadline_covers_response_body(db, fake, monkeypatch):
    monkeypatch.setattr(ledger_display, "_REQUEST_TIMEOUT", 0.2)
    fake.gate = threading.Event()
    started = time.monotonic()
    try:
        assert ledger_display._fetch_printers() == (False, {})
        wait_for_refresh()
        assert time.monotonic() - started < 1.0
        assert len(fake.calls) == 1
        assert ledger_display._cached_available is False
    finally:
        fake.gate.set()


def test_response_size_is_capped_before_parsing(db, fake):
    fake.chunks = [b" " * (ledger_display._MAX_RESPONSE_BYTES + 1)]
    assert refresh_and_wait() == (False, {})
    assert ledger_display._cached_available is False


def test_cache_expiry_is_measured_from_fetch_completion(fake, monkeypatch):
    monkeypatch.setattr(ledger_display, "_CACHE_SECONDS", 10.0)
    fake.delay = 0.2
    started = time.monotonic()
    assert ledger_display._fetch_printers() == (False, {})
    wait_for_refresh()
    completed = time.monotonic()
    with ledger_display._cache_lock:
        assert ledger_display._cached_available is True
        cached_until = ledger_display._cached_until
    assert completed - started >= 0.15
    # A start-relative expiry would be started + 10, roughly 0.2s earlier.
    assert cached_until >= completed + 9.9
    assert cached_until >= started + 10.1


def test_timed_out_dns_does_not_accumulate_transport_workers(fake, monkeypatch):
    monkeypatch.setattr(ledger_display, "_REQUEST_TIMEOUT", 0.05)
    gate = threading.Event()
    started = threading.Event()
    calls = []

    @contextmanager
    def slow_pin(endpoint):
        calls.append(endpoint)
        started.set()
        gate.wait(5)
        yield ["192.168.1.42"]

    monkeypatch.setattr(ledger_display, "pinned_device_destination", slow_pin)
    try:
        assert ledger_display._fetch_within_deadline("http://ledger") == (False, {})
        assert started.is_set()
        for _ in range(5):
            assert ledger_display._fetch_within_deadline("http://ledger") == (False, {})
        assert len(calls) == 1
    finally:
        gate.set()
        assert ledger_display._transport_lock.acquire(timeout=2)
        ledger_display._transport_lock.release()
