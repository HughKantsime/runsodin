"""Trusted service bootstrap for independently supervised monitor processes."""
from core.registry import registry


def bootstrap_monitor_authority() -> None:
    """Fail before connections or lifecycle writes if required authority is absent."""
    try:
        from modules.orders import register_providers as register_order_providers

        register_order_providers(registry)
    except Exception:
        raise RuntimeError("Monitor production authority initialization failed") from None
    try:
        from modules.organizations import register_providers
        from core.interfaces.education_policy import EducationPolicyProvider

        register_providers(registry)
        policy = registry.get_provider("EducationPolicyProvider")
        if not isinstance(policy, EducationPolicyProvider):
            raise TypeError("Invalid Education policy")
    except Exception:
        raise RuntimeError("Monitor Education authority initialization failed") from None
    try:
        from core.db import engine
        from core.power_target_operations import _history

        # Independently supervised monitors must validate recorded 027 storage
        # before installing any power producer. Never resume or clear a row.
        with engine.begin() as conn:
            _history(conn)
    except Exception:
        raise RuntimeError("Monitor power storage validation failed") from None
    try:
        from core.event_bus import get_event_bus
        from modules.printers import register_subscribers

        register_subscribers(get_event_bus())
    except Exception:
        raise RuntimeError("Monitor power event initialization failed") from None
