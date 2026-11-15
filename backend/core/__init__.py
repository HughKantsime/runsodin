"""ITAR mode: proxy settings are removed from the environment before any ODIN code runs.

Every ODIN process imports this package or `modules` first, so this is the one place that
covers them all. See core.itar.neutralize_environment_proxies. Does nothing with ITAR mode off.
"""
from .itar import neutralize_environment_proxies, refuse_malformed_itar_flag

refuse_malformed_itar_flag()  # a value that is neither on nor off stops the process here
neutralize_environment_proxies()
