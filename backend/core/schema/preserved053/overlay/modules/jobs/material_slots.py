"""Lock current tray bindings before manual completion accounting."""
from core.interfaces.inventory_moves import inventory_moves
from modules.printers.models import FilamentSlot


def locked_printer_slots(db, printer_id):
    moves = inventory_moves()
    slot_ids = db.query(FilamentSlot.id).filter(
        FilamentSlot.printer_id == printer_id).order_by(FilamentSlot.id).all()
    for slot_id, in slot_ids:
        moves.lock_slot_graph(db, slot_id)
    return db.query(FilamentSlot).filter(
        FilamentSlot.printer_id == printer_id).populate_existing().all()
