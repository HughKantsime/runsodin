"""ITAR mode: proxy settings are removed from the environment before any ODIN code runs.

`python3 -m modules.x` imports this package before the first line of `x`, so every monitor
and worker is covered here. See core.itar.neutralize_environment_proxies. Does nothing with
ITAR mode off.
"""
try:
    from core.itar import neutralize_environment_proxies
except ImportError:  # imported as backend.modules (ops/demo/demo_publisher.py)
    from ..core.itar import neutralize_environment_proxies

neutralize_environment_proxies()
