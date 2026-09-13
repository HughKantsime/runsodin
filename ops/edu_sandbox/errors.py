"""Typed lifecycle failures safe to show to an operator."""


class SandboxError(RuntimeError):
    """Base error for a rejected or failed sandbox operation."""


class ValidationError(SandboxError):
    """Untrusted operator input failed validation."""


class StateError(SandboxError):
    """The requested lifecycle transition is not legal."""


class OwnershipError(SandboxError):
    """A resource cannot be proven to belong to the requested sandbox."""


class SecretInputError(SandboxError):
    """A secret input channel or payload violated the bounded-input policy."""
