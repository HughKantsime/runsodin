#!/usr/bin/env python3
"""Offline contract tests for Odin's read-only Filament Ledger display patch."""

import importlib
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

BACKEND = Path(os.environ.get("ODIN_TEST_BACKEND", "")) if os.environ.get("ODIN_TEST_BACKEND") else None
if BACKEND is None:
    candidate = Path(__file__).resolve().parents[1]
    if (candidate / "core/db.py").exists():
        BACKEND = candidate
    elif Path("/app/backend/core/db.py").exists():
        BACKEND = Path("/app/backend")
    else:
        raise SystemExit("Set ODIN_TEST_BACKEND to the patched Odin backend source directory")
if not (BACKEND / "modules/printers/ledger_display.py").exists():
    raise SystemExit(f"Filament Ledger patch is not installed in {BACKEND}")

sys.path.insert(0, str(BACKEND))
scratch = tempfile.TemporaryDirectory(prefix="odin-ledger-test-")
os.environ["DATABASE_URL"] = "sqlite:///" + str(Path(scratch.name) / "odin-test.db")
os.environ["JWT_SECRET_KEY"] = "synthetic-ledger-test-secret-only"

from core.db import SessionLocal, engine
from core.schema.bootstrap import bootstrap_database
from sqlalchemy import text

for module_file in Path(BACKEND / "modules").glob("*/models.py"):
    importlib.import_module(".".join(module_file.relative_to(BACKEND).with_suffix("").parts))
from core.config import settings
from modules.inventory.routes._helpers import ScanAssignRequest
from modules.inventory.routes.filament_slots import assign_spool_to_slot, confirm_slot_assignment
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
from modules.printers.routes_filament_slots import AssignSpoolRequest, assign_spool_to_slot as agent_assign
from modules.printers.schemas import FilamentSlotUpdate, PrinterUpdate


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


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeClient:
    calls = []
    next_response = None
    init_kwargs = None

    def __init__(self, **kwargs):
        type(self).init_kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, url):
        type(self).calls.append(url)
        if type(self).next_response is None:
            return FakeResponse(ledger_payload())
        if isinstance(type(self).next_response, Exception):
            raise type(self).next_response
        return type(self).next_response


class FilamentLedgerDisplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bootstrap_database(engine, BACKEND)

    def setUp(self):
        with engine.begin() as conn:
            for table in ("filament_slots", "spools", "filament_library", "printers"):
                conn.execute(text("DELETE FROM " + table))
            conn.execute(text("""
                INSERT INTO printers(id,name,slot_count,is_active,api_type,api_host,tags,timelapse_enabled,shared,org_id)
                VALUES(7,'Ledger U1',4,1,'moonraker','u1.example.test:7125','[]',0,0,12),
                      (8,'Local printer',1,1,NULL,NULL,'[]',0,0,13)
            """))
            conn.execute(text("""
                INSERT INTO filament_slots(printer_id,slot_number,filament_type,color,color_hex,spoolman_spool_id,assigned_spool_id,spool_confirmed)
                VALUES(7,1,'PLA','old local value','#000000',7654,NULL,0),
                      (8,1,'PETG','local value','#ffffff',NULL,NULL,0)
            """))
            conn.execute(text("""
                INSERT INTO filament_library(id,brand,name,material,color_hex) VALUES(3,'Local','Test','PLA','#112233')
            """))
            conn.execute(text("""
                INSERT INTO spools(id,filament_id,qr_code,initial_weight_g,remaining_weight_g,spool_weight_g,status,location_printer_id,location_slot)
                VALUES(3,3,'synthetic-qr',1000,900,250,'active',NULL,NULL)
            """))
        settings.filament_ledger_url = "http://127.0.0.1:9876"
        settings.filament_ledger_printers = {7: "u1"}
        self.reset_cache()
        FakeClient.calls = []
        FakeClient.next_response = None
        FakeClient.init_kwargs = None
        self.client_patch = patch.object(ledger_display.httpx, "Client", FakeClient)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.pin_patch = patch.object(ledger_display, "pin_for_request")
        self.pin_patch.start()
        self.addCleanup(self.pin_patch.stop)
        self.addCleanup(self.reset_cache)

    @staticmethod
    def reset_cache():
        with ledger_display._cache_lock:
            ledger_display._cached_url = None
            ledger_display._cached_until = 0
            ledger_display._cached_printers = None
            ledger_display._cached_available = False

    def db_snapshot(self):
        with engine.connect() as conn:
            slots = conn.execute(text("SELECT printer_id,slot_number,filament_type,color,color_hex,spoolman_spool_id,assigned_spool_id,spool_confirmed FROM filament_slots ORDER BY printer_id,slot_number")).all()
            spools = conn.execute(text("SELECT id,remaining_weight_g,location_printer_id,location_slot FROM spools ORDER BY id")).all()
        return slots, spools

    def test_unconfigured_rows_are_same_orm_and_do_not_fetch(self):
        with SessionLocal() as db:
            row = db.query(__import__("modules.printers.models", fromlist=["Printer"]).Printer).filter_by(id=8).one()
            result = ledger_display.enrich_printers([row])
        self.assertIs(result[0], row)
        self.assertEqual(FakeClient.calls, [])

    def test_configured_enrichment_maps_zero_based_tools_and_never_writes(self):
        before = self.db_snapshot()
        with SessionLocal() as db:
            rows = db.query(__import__("modules.printers.models", fromlist=["Printer"]).Printer).order_by("id").all()
            result = ledger_display.enrich_printers(rows)
        self.assertEqual(result[0].filament_source, "filament-ledger")
        self.assertEqual(result[0].filament_source_status, "partial")
        self.assertEqual(result[0].filament_slots[0].slot_number, 1)
        self.assertIn(result[0].filament_slots[0].filament_type.value, ("OTHER", "PLA+"))
        self.assertEqual(result[0].filament_slots[0].material_type, "PLA+")
        self.assertEqual(result[0].filament_slots[0].color, "Example Maker Matte Blue")
        self.assertEqual(result[0].filament_slots[0].display_name, "Tool 1")
        self.assertEqual(result[0].filament_slots[0].color_hex, "1A2B3C")
        self.assertEqual(result[0].filament_slots[0].remaining, 100.0)
        self.assertEqual(result[0].filament_slots[0].external_spool_id, 8301)
        self.assertIsNone(result[0].filament_slots[0].spoolman_spool_id)
        self.assertIsNone(result[0].filament_slots[0].assigned_spool_id)
        self.assertEqual(result[0].filament_slots[1].mapping_status, "unmapped")
        self.assertEqual(result[0].filament_slots[1].filament_type.value, "Unknown")
        self.assertEqual(result[1], rows[1])
        self.assertEqual(len(FakeClient.calls), 1)
        self.assertEqual(FakeClient.calls[0], "http://127.0.0.1:9876/api/filament-display")
        self.assertEqual(FakeClient.init_kwargs, {"timeout": 2.0, "follow_redirects": False, "trust_env": False})
        self.assertEqual(self.db_snapshot(), before)

    def test_unmapped_tools_remain_explicit_without_inventing_material(self):
        FakeClient.next_response = FakeResponse(ledger_payload(tools=[{
            "tool_index": 0, "display_name": "Tool 1", "spool_id": None,
            "spool_status": "unmapped", "spool": None, "observed_filament": None,
        }]))
        with SessionLocal() as db:
            row = db.query(__import__("modules.printers.models", fromlist=["Printer"]).Printer).filter_by(id=7).one()
            result = ledger_display.enrich_printers([row])[0]
        self.assertEqual(result.filament_source_status, "partial")
        self.assertEqual(result.filament_slots[0].mapping_status, "unmapped")
        self.assertEqual(result.filament_slots[0].filament_type.value, "Unknown")
        self.assertIsNone(result.filament_slots[0].color)
        self.assertIsNone(result.filament_slots[0].remaining)

    def test_empty_tools_and_missing_printer_are_unavailable(self):
        for payload in (ledger_payload(tools=[]), {"printers": []}):
            self.reset_cache()
            FakeClient.next_response = FakeResponse(payload)
            with SessionLocal() as db:
                row = db.query(__import__("modules.printers.models", fromlist=["Printer"]).Printer).filter_by(id=7).one()
                result = ledger_display.enrich_printers([row])[0]
            self.assertEqual(result.filament_source_status, "unavailable")
            self.assertEqual(result.filament_slots, [])

    def test_provider_failure_and_bad_payload_back_off_and_hide_stale_local_slots(self):
        before = self.db_snapshot()
        for response in (FakeResponse({"bad": []}), FakeResponse({"printers": "bad"}, status=200), FakeResponse({}, status=503)):
            self.reset_cache()
            FakeClient.calls = []
            FakeClient.next_response = response
            with SessionLocal() as db:
                row = db.query(__import__("modules.printers.models", fromlist=["Printer"]).Printer).filter_by(id=7).one()
                result1 = ledger_display.enrich_printers([row])[0]
                result2 = ledger_display.enrich_printers([row])[0]
            self.assertEqual(result1.filament_source_status, "unavailable")
            self.assertEqual(result1.filament_slots, [])
            self.assertEqual(result2.filament_slots, [])
            self.assertEqual(len(FakeClient.calls), 1)
        self.assertEqual(self.db_snapshot(), before)

    def test_malformed_mapped_details_fail_closed_without_raising(self):
        malformed = ledger_payload(tools=[{
            "tool_index": 0, "display_name": "Tool 1", "spool_id": 5,
            "spool_status": "mapped", "spool": {"id": 5, "color_hex": "#nothex"},
        }])
        FakeClient.next_response = FakeResponse(malformed)
        with SessionLocal() as db:
            row = db.query(__import__("modules.printers.models", fromlist=["Printer"]).Printer).filter_by(id=7).one()
            result = ledger_display.enrich_printers([row])[0]
        self.assertEqual(result.filament_source_status, "unavailable")
        self.assertEqual(result.filament_slots, [])

    def test_read_routes_enrich_after_org_filter_and_keep_local_rows(self):
        user = {"role": "viewer", "group_id": 12}
        before = self.db_snapshot()
        with SessionLocal() as db:
            results = list_printers(False, None, None, user, {}, db)
            self.assertEqual([r.id for r in results], [7])
            self.assertEqual(results[0].filament_source, "filament-ledger")
            with self.assertRaises(Exception) as caught:
                get_printer(8, current_user={"role": "viewer", "group_id": 12}, _agent_scope={}, db=db)
            self.assertEqual(getattr(caught.exception, "status_code", None), 404)
            with self.assertRaises(Exception) as caught:
                list_filament_slots(7, {"role": "viewer", "group_id": 13}, db)
            self.assertEqual(getattr(caught.exception, "status_code", None), 404)
            slots = list_filament_slots(7, user, db)
            self.assertEqual(slots[0].display_name, "Tool 1")
        self.assertEqual(len(FakeClient.calls), 1)
        self.assertEqual(self.db_snapshot(), before)

    def test_local_filament_mutation_routes_reject_configured_printer(self):
        db = SessionLocal()
        user = {"role": "admin", "group_id": None}
        with self.assertRaises(Exception) as err:
            update_filament_slot(7, 1, FilamentSlotUpdate(color="changed"), user, db)
        self.assertEqual(err.exception.status_code, 409)
        with self.assertRaises(Exception) as err:
            sync_ams_state(7, user, db)
        self.assertEqual(err.exception.status_code, 409)
        with self.assertRaises(Exception) as err:
            assign_spool_to_slot(7, 1, 3, user, db)
        self.assertEqual(err.exception.status_code, 409)
        with self.assertRaises(Exception) as err:
            confirm_slot_assignment(7, 1, user, db)
        self.assertEqual(err.exception.status_code, 409)
        with self.assertRaises(Exception) as err:
            scan_assign_spool(ScanAssignRequest(qr_code="synthetic-qr", printer_id=7, slot=1), user, db)
        self.assertEqual(err.exception.status_code, 409)
        with self.assertRaises(Exception) as err:
            load_spool(3, SpoolLoadRequest(printer_id=7, slot_number=1), user, db)
        self.assertEqual(err.exception.status_code, 409)
        with self.assertRaises(Exception) as err:
            agent_assign(AssignSpoolRequest(spool_id=3, printer_id=7, ams_slot=1), None, user, {}, db)
        self.assertEqual(err.exception.status_code, 409)
        with self.assertRaises(Exception) as err:
            asyncio.run(manual_slot_assignment(7, 1, ManualSlotAssignment(color="changed"), user, db))
        self.assertEqual(err.exception.status_code, 409)
        with self.assertRaises(Exception) as err:
            update_printer(7, PrinterUpdate(slot_count=5), user, db)
        self.assertEqual(err.exception.status_code, 409)
        db.rollback()
        db.close()

    def test_local_mutations_remain_available_for_unconfigured_printer(self):
        db = SessionLocal()
        user = {"role": "admin", "group_id": None}
        result = update_filament_slot(8, 1, FilamentSlotUpdate(color="edited"), user, db)
        self.assertEqual(result.color, "edited")
        db.rollback()
        db.close()

    def test_no_binding_disables_provider_calls_and_guard(self):
        settings.filament_ledger_printers = {}
        before = self.db_snapshot()
        with SessionLocal() as db:
            row = db.query(__import__("modules.printers.models", fromlist=["Printer"]).Printer).filter_by(id=7).one()
            self.assertIs(ledger_display.enrich_printers([row])[0], row)
            result = update_filament_slot(7, 1, FilamentSlotUpdate(color="ok"), {"role": "admin", "group_id": None}, db)
            self.assertEqual(result.color, "ok")
            db.rollback()
        self.assertEqual(FakeClient.calls, [])
        self.assertNotEqual(self.db_snapshot(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
