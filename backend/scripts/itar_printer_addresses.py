"""List, change or clear the saved printer addresses that stop O.D.I.N. from starting in ITAR mode.

With ODIN_ITAR_MODE on, O.D.I.N. refuses to start while a saved printer has an IP address
outside the local network. The server is not running then, so the address cannot be fixed
in the UI, and starting once with ITAR mode off would start the printer monitors, which
would contact that address. This command fixes it without starting anything: it only reads
and writes the database. It opens no network connection and looks no name up.

usage (from /app/backend in the container; see docs/ITAR_MODE.md for the docker command):
  python3 -m scripts.itar_printer_addresses list
  python3 -m scripts.itar_printer_addresses set PRINTER_ID ADDRESS
  python3 -m scripts.itar_printer_addresses clear PRINTER_ID

  list   print each saved printer whose address is an IP address outside the local network.
  set    save ADDRESS (host or host:port) for the printer. An IP address must be on the local
         network. A host name is accepted without being looked up; it is checked every time
         O.D.I.N. connects to the printer.
  clear  remove the printer's address. The printer stays in O.D.I.N. with no address (it is
         not monitored) until one is entered in the UI.

`set` and `clear` write an audit entry (itar.printer_address_changed) in the same transaction
and read the row back before reporting success. Running a command twice gives the same result.

exit status: 0 done (list: no such printer is left), 1 list found at least one printer,
2 wrong usage, unknown printer or refused address, 3 the database could not be read or written.
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import text


def _outside_local_network(api_host) -> bool:
    """The same rule as the start-up check (core.itar.saved_printers_outside_local_network)."""
    from core.itar import _is_local_address, _literal_address
    from core.webhook_utils import DeviceDestinationBlocked, split_device_host
    try:
        host, _port = split_device_host(str(api_host or ""))
    except DeviceDestinationBlocked:
        return False
    address = _literal_address(host)
    return address is not None and not _is_local_address(address)


def list_printers(db, out) -> int:
    rows = db.execute(text("SELECT id, name, api_host FROM printers ORDER BY id")).fetchall()
    found = [row for row in rows if _outside_local_network(row[2])]
    for printer_id, name, api_host in found:
        print(f"printer {printer_id} ({name}): {api_host}", file=out)
    if not found:
        print("No saved printer has an address outside the local network.", file=out)
    return 1 if found else 0


def change_address(db, printer_id: int, address, out) -> int:
    """Save `address` (None clears it) for one printer. Returns the exit status."""
    from core.audit import log_audit
    from core.webhook_utils import DeviceDestinationBlocked, normalize_device_host

    row = db.execute(text("SELECT name, api_host FROM printers WHERE id = :id"), {"id": printer_id}).fetchone()
    if row is None:
        print(f"There is no printer {printer_id}.", file=out)
        return 2
    name, before = row
    after = None
    if address is not None:
        try:
            after = normalize_device_host(address)
        except (DeviceDestinationBlocked, UnicodeError):
            print("That is not an address: use a host or host:port.", file=out)
            return 2
        if _outside_local_network(after):
            print("That address is outside the local network; ITAR mode would refuse it again.", file=out)
            return 2
    if before == after:
        print(f"printer {printer_id} ({name}): already {'no address' if after is None else after}.", file=out)
        return 0
    db.execute(text("UPDATE printers SET api_host = :host WHERE id = :id"), {"host": after, "id": printer_id})
    log_audit(db, "itar.printer_address_changed", "printer", printer_id,
              {"from": before, "to": after, "source": "scripts.itar_printer_addresses"})
    db.commit()
    saved = db.execute(text("SELECT api_host FROM printers WHERE id = :id"), {"id": printer_id}).scalar()
    if saved != after:
        print(f"printer {printer_id} ({name}): the change was not saved.", file=out)
        return 3
    print(f"printer {printer_id} ({name}): {'address removed' if after is None else 'address is now ' + after}.",
          file=out)
    return 0


def main(argv=None, *, session_factory=None, out=None) -> int:
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="python3 -m scripts.itar_printer_addresses",
                                     description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="printers whose address is outside the local network")
    change = commands.add_parser("set", help="save a new address for a printer")
    change.add_argument("printer_id", type=int)
    change.add_argument("address")
    clear = commands.add_parser("clear", help="remove a printer's address")
    clear.add_argument("printer_id", type=int)
    try:
        arguments = parser.parse_args(argv)
    except SystemExit as stop:
        return 0 if stop.code == 0 else 2
    if session_factory is None:
        from core.db import SessionLocal as session_factory
    try:
        db = session_factory()
    except Exception as exc:
        print(f"The database could not be opened: {type(exc).__name__}", file=out)
        return 3
    try:
        if arguments.command == "list":
            return list_printers(db, out)
        return change_address(db, arguments.printer_id,
                              arguments.address if arguments.command == "set" else None, out)
    except Exception as exc:
        db.rollback()
        print(f"The database could not be read or written: {type(exc).__name__}", file=out)
        return 3
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
