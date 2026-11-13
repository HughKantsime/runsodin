"""ITAR mode: proxy settings are removed from the environment before any ODIN code runs.

Every ODIN process imports this package or `modules` first, so this is the one place that
covers them all. See core.itar.neutralize_environment_proxies. Does nothing with ITAR mode off.
"""
from .itar import neutralize_environment_proxies

neutralize_environment_proxies()
