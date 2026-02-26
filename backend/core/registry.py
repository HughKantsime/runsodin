# core/registry.py — Module Registry for dependency injection
#
# Tracks interface providers registered by modules. Supports validation that
# all declared REQUIRES dependencies have been satisfied before app startup.

import logging
from typing import Any

log = logging.getLogger("odin.registry")


class ModuleRegistry:
    """
    Lightweight dependency injection registry.

    Modules call register_provider() to advertise what interfaces they implement.
    Other modules call get_provider() to retrieve an implementation.
    validate_dependencies() checks that every REQUIRES declaration across all
    loaded modules has a matching registered provider.
    """

    def __init__(self):
        self._providers: dict[str, Any] = {}
        # Collect all REQUIRES declarations during module loading so we can
        # validate them at the end of the loading sequence.
        self._declared_requires: list[tuple[str, str]] = []  # (module_id, interface)

    def register_provider(self, interface_name: str, impl: Any) -> None:
        """Register an implementation for the named interface.

        Overwrites any previous registration (last writer wins). Logs a warning
        if the interface was already registered by a different object.
        """
        if interface_name in self._providers:
            existing = self._providers[interface_name]
            if existing is not impl:
                log.warning(
                    f"Interface '{interface_name}' already registered by "
                    f"{type(existing).__name__!r}; overwriting with "
                    f"{type(impl).__name__!r}"
                )
        self._providers[interface_name] = impl
        log.debug(f"Registered provider for '{interface_name}': {type(impl).__name__}")

    def get_provider(self, interface_name: str) -> Any:
        """Return the registered provider for an interface.

        Returns None (rather than raising) so callers can gracefully handle
        optional providers. Logs a warning when the provider is missing so
        operators notice misconfigurations early.
        """
        provider = self._providers.get(interface_name)
        if provider is None:
            log.warning(
                f"No provider registered for interface '{interface_name}'. "
                "Check that the required module is loaded."
            )
        return provider

    def record_requires(self, module_id: str, requires: list[str]) -> None:
        """Record the REQUIRES list for a module so validate_dependencies() can check it."""
        for iface in requires:
            self._declared_requires.append((module_id, iface))

    def validate_dependencies(self) -> bool:
        """Check that all REQUIRES declarations have registered providers.

        Returns True if all dependencies are satisfied. Logs errors for any
        unsatisfied dependencies but does not raise — the app factory decides
        whether to abort or continue.
        """
        missing: list[tuple[str, str]] = []
        for module_id, interface_name in self._declared_requires:
            if interface_name not in self._providers:
                missing.append((module_id, interface_name))

        if missing:
            for module_id, interface_name in missing:
                log.error(
                    f"Unsatisfied dependency: module '{module_id}' requires "
                    f"'{interface_name}' but no provider is registered."
                )
            return False

        log.info(
            f"All module dependencies satisfied "
            f"({len(self._declared_requires)} declarations checked)."
        )
        return True

    @property
    def providers(self) -> dict[str, Any]:
        """Read-only view of all registered providers."""
        return dict(self._providers)
