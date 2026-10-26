"""Printer implementation of the outside-print binding boundary."""
from core.interfaces.printer_binding import PrinterResourceRefused


class PrinterBindingService:
    def configuration_key(self, db, printer_id):
        from modules.printers.dispatch import _get_printer_info
        from core.dispatch_identity import build_identity
        from core.auth import SECRET_KEY
        identity = build_identity(_get_printer_info(printer_id, connection=db), SECRET_KEY.encode())
        return identity.configuration_key if identity is not None else None

    def lock_resource(self, view, table, identifier, column):
        from modules.printers.dispatch_admission import _lock, _Refused
        try:
            _lock(view, table, identifier, column)
        except _Refused as exc:
            raise PrinterResourceRefused(str(exc)) from exc
