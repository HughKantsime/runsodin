"""Inventory RFID policy provider for API and standalone monitor callers.

The provider leaves graph locks and transactions with its callers. Standalone
monitors use the same composition pattern as inventory_moves without API startup.
"""
from core.registry import registry

INTERFACE_NAME = 'RfidIdentityProvider'


def rfid_identity():
    if registry.has_provider(INTERFACE_NAME):
        return registry.get_provider(INTERFACE_NAME)
    from modules.inventory import rfid_identity as provider
    return provider
