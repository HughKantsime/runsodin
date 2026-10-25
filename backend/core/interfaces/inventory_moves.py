"""Inventory move interface available to standalone telemetry daemons.

The composition root is lazy because daemons don't construct the API registry.
All implementations leave locks, transaction ownership, and commit to callers.
"""
from typing import Protocol


class InventoryMoves(Protocol):
    def observe_empty_slots(self, connection, printer_id, numbers): ...
    def observe_slots(self, db, printer_id, observations, incoming_ids, current_user): ...
    def observed_departures(self, conn, printer_id, observations, incoming_ids): ...
    def break_seal(self, conn, spool_id): ...


def inventory_moves() -> InventoryMoves:
    from modules.inventory.slot_assignments import TelemetryInventoryMoves
    return TelemetryInventoryMoves()
