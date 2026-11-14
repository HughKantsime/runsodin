"""Recovery when ITAR mode refuses to start because a saved printer has a public address.

Review 2026-11-13 finding 9: the refusal told the operator to turn ITAR mode off, fix the
printer and turn it back on. Starting with ITAR mode off starts the printer monitors, which
contact the public address before it is corrected. scripts/itar_printer_addresses.py fixes
the address in the database with nothing running: no connection, no name lookup.

The database is a real SQLite database with ODIN's schema (the review_db fixture).
"""
import io
import socket
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from core import itar
from scripts import itar_printer_addresses as tool
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = "8.8.8.8"


@pytest.fixture
def farm(review_db, monkeypatch):
    """Printer 9 on the local network, 21 on a public address, 22 public with a port, 23 by name."""
    for printer_id, name, host in ((21, "Remote X1C", PUBLIC), (22, "Remote Klipper", "203.0.113.7:7125"),
                                   (23, "By name", "printer.plant.example")):
        review_db.execute(text(
            "INSERT INTO printers(id,name,model,is_active,api_type,api_host,api_key,org_id) "
            "VALUES(:id,:name,'X1C',1,'bambu',:host,'do-not-print-this-key',1)"),
            {"id": printer_id, "name": name, "host": host})
    review_db.execute(text("UPDATE printers SET api_host='192.168.1.50' WHERE id=9"))
    review_db.commit()
    factory = sessionmaker(bind=review_db.get_bind())

    def no_network(*args, **kwargs):
        raise AssertionError("the recovery command used the network")
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)

    def run(*argv):
        out = io.StringIO()
        return tool.main(list(argv), session_factory=factory, out=out), out.getvalue()

    def host(printer_id):
        review_db.expire_all()
        return review_db.execute(text("SELECT api_host FROM printers WHERE id=:id"), {"id": printer_id}).scalar()
    return run, host, review_db


def test_list_names_the_printers_the_start_up_check_refuses(farm, monkeypatch):
    run, _host, db = farm
    code, output = run("list")
    assert code == 1
    assert output.splitlines() == [f"printer 21 (Remote X1C): {PUBLIC}", "printer 22 (Remote Klipper): 203.0.113.7:7125"]
    assert "do-not-print-this-key" not in output
    # exactly the printers the start-up check names
    import core.db
    monkeypatch.setattr(core.db, "SessionLocal", sessionmaker(bind=db.get_bind()))
    assert itar.saved_printers_outside_local_network() == ["printer 21 (Remote X1C)", "printer 22 (Remote Klipper)"]


def test_set_and_clear_fix_the_addresses_and_the_start_up_check_then_passes(farm, monkeypatch):
    run, host, db = farm
    assert run("set", "21", "192.168.1.60") == (0, "printer 21 (Remote X1C): address is now 192.168.1.60.\n")
    assert run("clear", "22") == (0, "printer 22 (Remote Klipper): address removed.\n")
    assert host(21) == "192.168.1.60" and host(22) is None
    assert run("list") == (0, "No saved printer has an address outside the local network.\n")
    import core.db
    monkeypatch.setattr(core.db, "SessionLocal", sessionmaker(bind=db.get_bind()))
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    itar.enforce_boot_printer_addresses()
    rows = db.execute(text("SELECT entity_id, details FROM audit_logs WHERE action='itar.printer_address_changed' "
                           "ORDER BY id")).fetchall()
    assert [row[0] for row in rows] == [21, 22]
    assert PUBLIC in str(rows[0][1]) and "192.168.1.60" in str(rows[0][1])


def test_running_a_command_twice_changes_nothing_more(farm):
    run, host, db = farm
    run("set", "21", "192.168.1.60")
    assert run("set", "21", "192.168.1.60") == (0, "printer 21 (Remote X1C): already 192.168.1.60.\n")
    run("clear", "22")
    assert run("clear", "22") == (0, "printer 22 (Remote Klipper): already no address.\n")
    assert db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='itar.printer_address_changed'")).scalar() == 2


@pytest.mark.parametrize("address", [PUBLIC, "1.1.1.1:7125", "[2001:4860:4860::8888]:80", "8.8", "http://10.0.0.5/x",
                                     "user@10.0.0.5", ""])
def test_set_refuses_a_public_or_malformed_address_and_changes_nothing(farm, address):
    run, host, _db = farm
    code, output = run("set", "21", address)
    assert code == 2 and host(21) == PUBLIC
    assert "outside the local network" in output or "not an address" in output


def test_set_accepts_a_local_address_with_a_port_ipv6_and_a_name_without_a_lookup(farm):
    run, host, _db = farm
    assert run("set", "21", "10.0.0.5:7125")[0] == 0 and host(21) == "10.0.0.5:7125"
    assert run("set", "21", "[fd12:3456::7]:80")[0] == 0 and host(21) == "[fd12:3456::7]:80"
    assert run("set", "21", "Printer-A.Plant.Example")[0] == 0 and host(21) == "printer-a.plant.example"


def test_wrong_usage_and_an_unknown_printer_exit_2(farm):
    run, host, _db = farm
    assert run("set", "999", "10.0.0.5") == (2, "There is no printer 999.\n")
    assert run("clear", "999")[0] == 2
    assert run()[0] == 2 and run("set", "21")[0] == 2 and run("remove", "21")[0] == 2 and run("set", "x", "10.0.0.5")[0] == 2
    assert host(21) == PUBLIC


def test_a_database_that_cannot_be_opened_exits_3():
    def broken():
        raise RuntimeError("no database")
    out = io.StringIO()
    assert tool.main(["list"], session_factory=broken, out=out) == 3
    assert "could not be opened" in out.getvalue()


def test_the_refusal_and_the_guide_point_to_the_command_and_not_to_turning_itar_off(monkeypatch):
    import core.db
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")

    class Rows:
        def execute(self, statement):
            from types import SimpleNamespace
            return SimpleNamespace(fetchall=lambda: [(2, "Remote X1C", PUBLIC)])

        def close(self):
            pass
    monkeypatch.setattr(core.db, "SessionLocal", Rows)
    with pytest.raises(RuntimeError) as refused:
        itar.enforce_boot_printer_addresses()
    message = str(refused.value)
    assert "scripts.itar_printer_addresses" in message and "docs/ITAR_MODE.md" in message
    assert "Unset ODIN_ITAR_MODE" not in message and PUBLIC not in message
    guide = (ROOT / "docs/ITAR_MODE.md").read_text(encoding="utf-8")
    section = guide[guide.index("## If O.D.I.N. refuses to start because of a saved printer"):]
    section = section[:section.index("\n## ", 5)]
    for needed in ("scripts.itar_printer_addresses list", "scripts.itar_printer_addresses set",
                   "scripts.itar_printer_addresses clear", "--network none", "--entrypoint"):
        assert needed in section, needed
    assert "Do not start O.D.I.N. with ITAR mode off" in section
