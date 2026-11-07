"""Printer implementation of the outside-print binding boundary."""
from core.interfaces.printer_binding import PrinterResourceRefused


class PrinterBindingService:
    def configuration_key(self, db, printer_id):
        from modules.printers.dispatch import _get_printer_info
        from core.dispatch_identity import build_identity
        from core.auth import SECRET_KEY
        identity = build_identity(_get_printer_info(printer_id, connection=db), SECRET_KEY.encode())
        return identity.configuration_key if identity is not None else None

    def recipe_configuration_key(self, db, printer_id, *, since=None):
        from sqlalchemy import text
        from core.recipe_configuration import unchanged_since
        from modules.printers.dispatch import _get_printer_info
        from core.dispatch_identity import stable_configuration_key
        from core.auth import SECRET_KEY
        if db.execute(text('UPDATE printers SET id=id WHERE id=:id'), {'id':printer_id}).rowcount != 1:
            return None
        if since is not None and not unchanged_since(db, printer_id, since):
            return None
        return stable_configuration_key(_get_printer_info(printer_id, connection=db), SECRET_KEY.encode())

    def lock_resource(self, view, table, identifier, column):
        from modules.printers.dispatch_admission import _lock, _Refused
        try:
            _lock(view, table, identifier, column)
        except _Refused as exc:
            raise PrinterResourceRefused(str(exc)) from exc
