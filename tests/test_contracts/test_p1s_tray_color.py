"""P1S slot colour after an accepted tray_set: the stored and served colour survives
P1S AMS reports that carry no colour, and a real colour change from the printer wins.

Report shapes follow the P1S unit fields recorded in telemetry/bambu/raw.py and the
"00000000" no-colour placeholder seen on loaded trays in the captured H2D/X1C frames.
"""
import copy
import sqlite3
from contextlib import closing

import pytest
from sqlalchemy import text

from tests.test_contracts.test_cert_A_harness import (  # noqa: F401
    ACCESS, SERIAL, broker_socket, cert_app, cert_client, fernet_env, review_db, wire_monitor,
)
from tests.test_contracts.test_cert_A_bambu_controls import TRAY_SETTINGS, tray_broker  # noqa: F401

# PUT /printers/301/ams/0/slots/2 -> slot 3, tray id "2".
TRAY_SET = {**TRAY_SETTINGS, "color": "898989FF"}


def p1s_tray(**fields):
    tray = {"id": "2", "tray_type": "PETG", "tray_sub_brands": "", "tray_info_idx": "GFG02",
            "tray_id_name": "", "tag_uid": "0000000000000000",
            "tray_uuid": "00000000000000000000000000000000", "tray_weight": "0", "tray_diameter": "1.75",
            "remain": -1, "nozzle_temp_min": "230", "nozzle_temp_max": "260", "bed_temp": "0",
            "bed_temp_type": "0", "k": 0.02, "n": 1, "cali_idx": -1, "ctype": 0, "state": 3}
    tray.update(fields)
    return {key: value for key, value in tray.items() if value is not None}


def p1s_units(tray):
    return [{"id": "0", "ams_id": "0", "check": 0, "chip_id": "", "dry_time": 0, "humidity": "4",
             "humidity_raw": "21", "info": "1001", "temp": "0.0", "tray": [tray]}]


def p1s_report(tray):
    return {"print": {"command": "push_status", "gcode_state": "IDLE", "bed_temper": 24.0,
                      "nozzle_temper": 25.0, "ams": {"ams": p1s_units(tray), "ams_exist_bits": "1",
                                                     "tray_exist_bits": "4", "version": 7}}}


NO_COLOUR_REPORTS = [
    pytest.param(dict(tray_color="00000000", cols=["00000000"]), id="placeholder-with-cols"),
    pytest.param(dict(tray_color="00000000"), id="placeholder"),
    pytest.param(dict(tray_color=None), id="absent"),
    pytest.param(dict(tray_color=""), id="empty"),
    pytest.param(dict(tray_color="zzzzzzzz"), id="malformed"),
]


def slot3(db, printer_id=301):
    db.expire_all()
    return tuple(db.execute(text("SELECT filament_type,color_hex FROM filament_slots "
                                 "WHERE printer_id=:p AND slot_number=3"), {"p": printer_id}).one())


@pytest.mark.parametrize("tray,expected", [
    (dict(tray_color="898989FF"), "898989"),
    (dict(tray_color="898989"), "898989"),
    (dict(tray_color="898989ff"), "898989"),
    (dict(tray_color="000000FF"), "000000"),  # opaque black is a real colour
    (dict(tray_color="00000000", cols=["898989FF"]), "898989"),
    (dict(cols=["898989FF"]), "898989"),
    (dict(tray_color="00000000", cols=["00000000"]), None),
    (dict(tray_color="00000000"), None),
    (dict(), None),
    (dict(tray_color=""), None),
    (dict(tray_color="#898989"), "898989"),
    (dict(tray_color="FFFFFF00"), None),  # any alpha-00 value is the no-colour placeholder
    (dict(tray_color="FFFFFF00", cols=["898989FF"]), "898989"),
    (dict(tray_color="748C4580"), "748C45"),  # translucent, not a placeholder
    (dict(tray_color="89898"), None),
    (dict(tray_color=0), None),
    (dict(tray_color="00000000", cols=[]), None),
    (dict(tray_color="00000000", cols="898989FF"), None),
])
def test_reported_color_reads_p1s_shapes(tray, expected):
    from modules.printers.telemetry.bambu.ams_identity import reported_color
    assert reported_color({"id": "2", "tray_type": "PETG", **tray}) == expected


def test_tray_set_records_accepted_colour_and_type_without_spool(cert_app, tray_broker, review_db):
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,color_hex) "
                           "VALUES(301,3,'PLA','FFFFFF')"))
    review_db.commit()
    response = cert_app.put("/printers/301/ams/0/slots/2", headers=cert_app.roles["operator"], json=TRAY_SET)
    assert response.status_code == 200, response.text
    assert slot3(review_db) == ("PETG", "898989")


def test_refused_tray_set_leaves_slot_unchanged(cert_app, broker_socket, review_db, monkeypatch):
    from modules.printers.telemetry.bambu import tray_setting
    monkeypatch.setattr(tray_setting, "set_tray", lambda *a, **k: {"result": "fail", "reason": "refused"})
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,color_hex) "
                           "VALUES(301,3,'PLA','FFFFFF')"))
    review_db.commit()
    response = cert_app.put("/printers/301/ams/0/slots/2", headers=cert_app.roles["operator"], json=TRAY_SET)
    assert response.status_code == 409, response.text
    assert slot3(review_db) == ("PLA", "FFFFFF")


def daemon_sync(review_db, units):
    """The daemon's AMS inventory write on its own raw connection, as mqtt_status runs it."""
    from core.audit_integrity import install_sqlite
    from modules.printers.monitors.ams_inventory import sync_ams_inventory
    from modules.printers.telemetry.bambu.ams_identity import inventory_report
    with closing(sqlite3.connect(review_db.get_bind().url.database)) as conn:
        install_sqlite(conn)
        sync_ams_inventory(conn, 301, inventory_report(units, "4"))
        conn.commit()


@pytest.mark.parametrize("report", NO_COLOUR_REPORTS)
def test_p1s_report_without_colour_keeps_tray_set_colour_served(cert_app, tray_broker, review_db, report):
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,color_hex) "
                           "VALUES(301,3,'PLA','FFFFFF')"))
    review_db.commit()
    assert cert_app.put("/printers/301/ams/0/slots/2", headers=cert_app.roles["operator"],
                        json=TRAY_SET).status_code == 200
    daemon_sync(review_db, p1s_units(p1s_tray(**report)))
    assert slot3(review_db) == ("PETG", "898989")
    printer = cert_app.get("/printers/301", headers=cert_app.roles["operator"])
    assert printer.status_code == 200, printer.text
    served = {s["slot_number"]: s for s in printer.json()["filament_slots"]}
    assert served[3]["color_hex"] == "898989"


def test_p1s_real_colour_change_from_printer_wins(cert_app, tray_broker, review_db):
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,color_hex) "
                           "VALUES(301,3,'PLA','FFFFFF')"))
    review_db.commit()
    assert cert_app.put("/printers/301/ams/0/slots/2", headers=cert_app.roles["operator"],
                        json=TRAY_SET).status_code == 200
    daemon_sync(review_db, p1s_units(p1s_tray(tray_color="FF0000FF", cols=["FF0000FF"])))
    assert slot3(review_db) == ("PETG", "FF0000")
    # A colour carried only in cols (tray_color placeholder) is still a real colour.
    daemon_sync(review_db, p1s_units(p1s_tray(tray_color="00000000", cols=["00AE42FF"])))
    assert slot3(review_db) == ("PETG", "00AE42")


def test_p1s_material_change_without_colour_does_not_keep_stale_colour(cert_app, tray_broker, review_db):
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,color_hex) "
                           "VALUES(301,3,'PETG','898989')"))
    review_db.commit()
    daemon_sync(review_db, p1s_units(p1s_tray(tray_type="PLA", tray_info_idx="GFA00", tray_color="00000000")))
    assert slot3(review_db) == ("PLA", None)


def test_p1s_empty_tray_still_clears_colour(cert_app, tray_broker, review_db):
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,color_hex) "
                           "VALUES(301,3,'PETG','898989')"))
    review_db.commit()
    daemon_sync(review_db, p1s_units(p1s_tray(tray_type="", tray_info_idx="", tray_color="00000000")))
    assert slot3(review_db) == ("empty", None)


@pytest.mark.parametrize("report", NO_COLOUR_REPORTS)
def test_p1s_wire_report_keeps_colour_then_real_change_wins(wire_monitor, review_db, report):
    """The shipped monitor pipeline: BambuPrinter._on_message -> PrinterMonitor._on_status -> sync."""
    monitor, sock, clock, _ = wire_monitor
    review_db.execute(text("UPDATE printers SET model='P1S',machine_type='P1S' WHERE id=301"))
    # The slot as the accepted tray_set leaves it.
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,color_hex) "
                           "VALUES(301,3,'PETG','898989')"))
    review_db.commit()
    clock[0] += 11
    sock.deliver(p1s_report(p1s_tray(**report)))
    assert monitor._pending_ams_report is None  # the report was synchronized
    assert slot3(review_db) == ("PETG", "898989")
    # P1S delta frames carry trays with only their id; they never touch the slot.
    clock[0] += 61
    sock.deliver(p1s_report({"id": "2"}))
    assert slot3(review_db) == ("PETG", "898989")
    clock[0] += 61
    sock.deliver(p1s_report(p1s_tray(tray_color="FF0000FF", cols=["FF0000FF"])))
    assert slot3(review_db) == ("PETG", "FF0000")


@pytest.mark.parametrize("value,expected", [
    ("898989", "898989"), ("#898989", "898989"), ("898989ff", "898989"), ("#898989FF", "898989"),
    ("00000000", None), ("89898900", None), ("red", None), ("", None), ("#12345", None),
    ("8989898", None), ("898989FFF", None), (None, None), (898989, None),
])
def test_hex_color_accepts_only_six_or_eight_digit_hex(value, expected):
    from modules.printers.telemetry.bambu.ams_identity import hex_color
    assert hex_color(value) == expected


@pytest.mark.parametrize("color", ["#898989", "898989ff", "#898989FF"])
def test_tray_set_normalises_accepted_colour_forms(cert_app, tray_broker, review_db, color):
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,color_hex) "
                           "VALUES(301,3,'PLA','FFFFFF')"))
    review_db.commit()
    response = cert_app.put("/printers/301/ams/0/slots/2", headers=cert_app.roles["operator"],
                            json={**TRAY_SET, "color": color})
    assert response.status_code == 200, response.text
    assert slot3(review_db) == ("PETG", "898989")


@pytest.mark.parametrize("color", ["red", "89898900", "#12345", "898989FFF"])
def test_tray_set_request_schema_refuses_invalid_colour_before_wire(cert_app, broker_socket, review_db, color):
    response = cert_app.put("/printers/301/ams/0/slots/2", headers=cert_app.roles["operator"],
                            json={**TRAY_SET, "color": color})
    assert response.status_code == 422, response.text


def test_tray_set_invalid_colour_after_acceptance_keeps_colour_without_failing(cert_app, tray_broker, review_db):
    """Defence in depth: a colour that bypassed the schema never fails the accepted command."""
    import asyncio
    from sqlalchemy.orm import Session
    from modules.printers.routes_ams_env import AMSSlotConfigRequest, configure_ams_slot
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,color_hex) "
                           "VALUES(301,3,'PLA','FFFFFF')"))
    review_db.commit()
    body = AMSSlotConfigRequest.model_construct(**{**TRAY_SET, "color": "not-a-colour"})
    with Session(review_db.get_bind()) as db:
        result = asyncio.run(configure_ams_slot(301, 0, 2, body,
                                                current_user={"id": 6, "role": "operator", "group_id": 1}, db=db))
    assert result["success"] is True
    assert slot3(review_db) == ("PETG", "FFFFFF")


def test_rfid_swap_same_material_with_placeholder_takes_new_spool_colour(cert_app, tray_broker, review_db):
    """A different RFID spool of the same material never inherits the previous spool's colour."""
    review_db.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(77,'Bambu Lab','PETG Basic','PETG')"))
    review_db.execute(text("INSERT INTO spools(id,filament_id,org_id,status,rfid_tag,tray_uuid,color_hex,location_printer_id,location_slot) VALUES "
                           "(71,77,1,'active','AAAAAAAAAAAAAAAA','" + "a" * 32 + "','FF0000',301,3),"
                           "(72,77,1,'active','BBBBBBBBBBBBBBBB','" + "b" * 32 + "','00FF00',NULL,NULL)"))
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,color_hex,assigned_spool_id,spool_confirmed) "
                           "VALUES(301,3,'PETG','FF0000',71,1)"))
    review_db.commit()
    rfid = dict(tray_sub_brands="PETG Basic", tray_color="00000000", cols=["00000000"])
    # Same spool, placeholder colour: the known colour stays.
    daemon_sync(review_db, p1s_units(p1s_tray(tag_uid="AAAAAAAAAAAAAAAA", tray_uuid="a" * 32, **rfid)))
    assert slot3(review_db) == ("PETG", "FF0000")
    # Swapped to a known spool of the same material: its own colour, not the stale one.
    daemon_sync(review_db, p1s_units(p1s_tray(tag_uid="BBBBBBBBBBBBBBBB", tray_uuid="b" * 32, **rfid)))
    assert slot3(review_db) == ("PETG", "00FF00")
    assert review_db.execute(text("SELECT assigned_spool_id FROM filament_slots WHERE printer_id=301 AND slot_number=3")).scalar_one() == 72
    # Swapped to an unknown RFID spool: created without a colour, so the slot has none.
    daemon_sync(review_db, p1s_units(p1s_tray(tag_uid="CCCCCCCCCCCCCCCC", tray_uuid="c" * 32, **rfid)))
    assert slot3(review_db) == ("PETG", None)
    created = review_db.execute(text("SELECT color_hex FROM spools WHERE rfid_tag='CCCCCCCCCCCCCCCC'")).one()
    assert created == (None,)


def test_rfid_spool_replaced_by_tagless_spool_drops_the_rfid_colour(cert_app, tray_broker, review_db):
    review_db.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(77,'Bambu Lab','PETG Basic','PETG')"))
    review_db.execute(text("INSERT INTO spools(id,filament_id,org_id,status,rfid_tag,tray_uuid,color_hex,location_printer_id,location_slot) VALUES "
                           "(71,77,1,'active','AAAAAAAAAAAAAAAA','" + "a" * 32 + "','FF0000',301,3)"))
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,color_hex,assigned_spool_id,spool_confirmed) "
                           "VALUES(301,3,'PETG','FF0000',71,1)"))
    review_db.commit()
    daemon_sync(review_db, p1s_units(p1s_tray(tag_uid="AAAAAAAAAAAAAAAA", tray_uuid="a" * 32, tray_sub_brands="PETG Basic",
                                              tray_color="FF0000FF")))
    daemon_sync(review_db, p1s_units(p1s_tray(tray_color="00000000")))
    assert slot3(review_db) == ("PETG", None)


def test_tagless_tray_with_unknown_identity_keeps_known_colour(cert_app, tray_broker, review_db):
    """Documented limitation: a tagless tray after a tagless tray has no identity to compare,
    so a different tagless spool of the same material keeps the known colour until the
    printer reports one (or tray_set records one)."""
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,color_hex) "
                           "VALUES(301,3,'PETG','898989')"))
    review_db.commit()
    daemon_sync(review_db, p1s_units(p1s_tray(tray_color="00000000")))
    daemon_sync(review_db, p1s_units(p1s_tray(tray_color="00000000", tray_info_idx="GFG99")))
    assert slot3(review_db) == ("PETG", "898989")


@pytest.mark.parametrize("args,expected", [
    (dict(tray={"tag_uid": "A" * 16}, observation={"observed": {"tag": "A" * 16}}, destination=None, previous_spool=None), False),
    (dict(tray={"tag_uid": "B" * 16}, observation={"observed": {"tag": "A" * 16}}, destination=None, previous_spool=None), True),
    (dict(tray={"tray_uuid": "b" * 32, "tag_uid": "A" * 16}, observation={"observed": {"uuid": "a" * 32, "tag": "A" * 16}}, destination=None, previous_spool=None), True),
    (dict(tray={}, observation={"observed": {"tag": "A" * 16}}, destination=None, previous_spool=None), True),
    (dict(tray={"tag_uid": "0" * 16}, observation={}, destination=None, previous_spool=None), False),
    (dict(tray={"tag_uid": "A" * 16}, observation={}, destination=5, previous_spool=5), False),
    (dict(tray={"tag_uid": "A" * 16}, observation={"observed": {"tag": "A" * 16}}, destination=6, previous_spool=5), True),
    (dict(tray={"tag_uid": "A" * 16}, observation=None, destination=6, previous_spool=None), True),
])
def test_tray_swapped_decisions(args, expected):
    from modules.printers.telemetry.bambu.ams_identity import tray_swapped
    assert tray_swapped(**args) is expected
