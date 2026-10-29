"""Attempt accounting access for API and standalone telemetry callers.

Like inventory_moves, this lazy composition works without API startup and
leaves transaction ownership with the caller.
"""


def attempt_accounting():
    from modules.archives import attempt_accounting as provider
    return provider
