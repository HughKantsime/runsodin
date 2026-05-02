"""Centralized resolver for Bambu MQTT broker connection policy.

**Intended for demo/CI compose stacks only.** Real Bambu printers always use
TLS on port 8883 — that's the only path their firmware exposes. The bypass
implemented here exists so that a `mosquitto` container inside an isolated
demo stack can be subscribed to by ODIN without forging a TLS cert chain.

## Design (Codex-vetted, ODIN-142 Wake 2b)

ONE helper, `resolve_bambu_broker_config(host)`, returns the
`(host, port, use_tls)` triple every `BambuAdapterConfig(...)` site uses.
Default behavior is **production-identical**: TLS on 8883. The bypass is
unreachable unless ALL of these are true at runtime:

  1. `ODIN_ALLOW_INSECURE_BAMBU_BROKER` is set to a truthy value.
  2. The requested host appears (case-insensitively) in the
     `ODIN_BAMBU_INSECURE_BROKER_HOSTS` comma-separated allowlist.
  3. `ODIN_ITAR_MODE` is NOT enabled (ITAR makes the bypass unreachable
     at runtime even if env config permits it; the boot audit also
     refuses to start in this configuration).

If any one is missing, the resolver returns `(host, 8883, True)`. There
is no per-printer override, no DB column, no API toggle. The only knob
is the deploy env, set once in compose / k8s / systemd.

## Boot audit (called from `core/app.py`)

`enforce_boot_audit()` runs at app startup and refuses to boot if:

  * `ODIN_ITAR_MODE=1` AND insecure-broker config is present.
  * `ODIN_BAMBU_INSECURE_BROKER_PORT` is set to a non-integer.
  * Any allowlisted host is a literal IP that resolves to a public
    address. (DNS-resolved hostnames aren't enforced at boot — the demo
    `mosquitto` lives on a kube-internal Service that won't resolve in a
    plain Python startup process. The allowlist itself is the trust
    boundary; ITAR mode is the kill-switch.)

## Audit logging

Every time the resolver returns `use_tls=False`, a structured warning is
emitted with `printer_id`, `host`, `port`, and `reason`. Serial numbers
and access codes are NEVER logged here — those are still considered
credentials.

## Anti-patterns this design rejects

  * **Design 1 (DB column).** A per-printer `use_tls` would make
    plaintext MQTT a persistent production feature with API/UI/migration
    blast radius. Rejected: keep it env-only.
  * **Design 3 (promote test harnesses).** `live_replay.LocalBroker` and
    `live_shadow.run_live_shadow` already disable TLS for tests. Don't
    conflate them with the production path.
  * **Glob/wildcard hosts in the allowlist.** Exact match only — globs
    let an operator accidentally bypass TLS for a wide blast radius.
"""

from __future__ import annotations

import ipaddress
import logging
import os
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("odin.bambu.broker_policy")


_ALLOW_FLAG_VALUES = {"1", "true", "yes", "on"}

# Defaults. Production path stays here.
DEFAULT_BAMBU_PORT = 8883
DEFAULT_BAMBU_USE_TLS = True
DEFAULT_INSECURE_PORT = 1883


@dataclass(frozen=True)
class BrokerEndpoint:
    """Resolver output. `as_tuple()` matches the legacy 3-tuple API."""
    host: str
    port: int
    use_tls: bool

    def as_tuple(self) -> tuple[str, int, bool]:
        return (self.host, self.port, self.use_tls)


def _truthy(val: Optional[str]) -> bool:
    if val is None:
        return False
    return val.strip().lower() in _ALLOW_FLAG_VALUES


def _read_allowlist() -> tuple[str, ...]:
    """Parse ODIN_BAMBU_INSECURE_BROKER_HOSTS into a tuple of lowercase
    host strings. Empty/missing → empty tuple."""
    raw = os.environ.get("ODIN_BAMBU_INSECURE_BROKER_HOSTS", "").strip()
    if not raw:
        return ()
    return tuple(h.strip().lower() for h in raw.split(",") if h.strip())


def _read_insecure_port() -> int:
    """Parse ODIN_BAMBU_INSECURE_BROKER_PORT, defaulting to 1883.

    Raises ValueError on a non-integer value — caller (boot audit) maps
    this to a hard boot failure so a misconfigured deploy fails loud.
    """
    raw = os.environ.get("ODIN_BAMBU_INSECURE_BROKER_PORT", "").strip()
    if not raw:
        return DEFAULT_INSECURE_PORT
    try:
        port = int(raw)
    except ValueError as e:
        raise ValueError(
            f"ODIN_BAMBU_INSECURE_BROKER_PORT must be an integer, got {raw!r}"
        ) from e
    if not (0 < port < 65536):
        raise ValueError(
            f"ODIN_BAMBU_INSECURE_BROKER_PORT out of range: {port}"
        )
    return port


def _itar_blocks_bypass() -> bool:
    """Read the ITAR flag every call so tests can flip it without restarts.

    Imported lazily so this module can be unit-tested without spinning up
    the full backend `core` package.
    """
    try:
        from core.itar import is_itar_mode
    except ImportError:
        return False
    return is_itar_mode()


def _is_private_or_loopback_literal(host: str) -> bool:
    """True iff `host` parses as a literal IP in RFC1918/loopback/link-local.

    Hostnames (e.g. `mosquitto`, `localhost`) return False here — they're
    handled by the allowlist trust boundary, not by IP-class checks.
    """
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_unspecified
    )


def resolve_bambu_broker_config(
    host: str,
    *,
    printer_id: Optional[str] = None,
) -> BrokerEndpoint:
    """Return `(host, port, use_tls)` for a BambuAdapterConfig.

    Production default: `(host, 8883, True)`. Bypass to plain `(host, 1883,
    False)` requires the global gate AND the per-host allowlist AND ITAR
    mode disabled.

    `printer_id` is used only in the structured audit log; it has no
    effect on the resolved endpoint. Pass it whenever the caller has it
    so post-incident triage can grep by printer.
    """
    if not host:
        return BrokerEndpoint(host=host, port=DEFAULT_BAMBU_PORT, use_tls=True)

    if _itar_blocks_bypass():
        # ITAR override — even if env permits it. Defense in depth: the
        # boot audit also refuses this combination, so reaching here
        # means a runtime env mutation. Fall back to TLS:8883 silently.
        return BrokerEndpoint(host=host, port=DEFAULT_BAMBU_PORT, use_tls=True)

    if not _truthy(os.environ.get("ODIN_ALLOW_INSECURE_BAMBU_BROKER")):
        return BrokerEndpoint(host=host, port=DEFAULT_BAMBU_PORT, use_tls=True)

    allowlist = _read_allowlist()
    if host.strip().lower() not in allowlist:
        return BrokerEndpoint(host=host, port=DEFAULT_BAMBU_PORT, use_tls=True)

    # All gates passed. Read the insecure port (default 1883) and emit a
    # structured warning so the bypass shows up in operator log review.
    try:
        port = _read_insecure_port()
    except ValueError:
        # Boot audit should have caught this; if it didn't, refuse to
        # downgrade. Fail-closed.
        log.error("bambu_broker_insecure_bypass_skipped_invalid_port host=%s", host)
        return BrokerEndpoint(host=host, port=DEFAULT_BAMBU_PORT, use_tls=True)

    log.warning(
        "bambu_broker_insecure_bypass printer_id=%s host=%s port=%d use_tls=false reason=insecure_broker_allowlist",
        printer_id or "unknown",
        host,
        port,
    )
    return BrokerEndpoint(host=host, port=port, use_tls=False)


def is_bypass_configured() -> bool:
    """True iff env config would permit the bypass (allowlist non-empty
    AND global gate set). Caller must still go through `resolve_bambu_broker_config`
    for actual resolution — this is just a boot-audit / observability hook.
    """
    if not _truthy(os.environ.get("ODIN_ALLOW_INSECURE_BAMBU_BROKER")):
        return False
    return bool(_read_allowlist())


def audit_boot_config() -> list[str]:
    """Inspect env config for misconfigurations. Returns a list of
    violation strings (empty = clean).

    Violations:
      * ITAR mode + insecure broker bypass configured.
      * Insecure broker port is set to a non-integer or out-of-range.
      * Any allowlisted host that's a literal IP outside RFC1918/loopback.

    Hostname allowlist entries (e.g. `mosquitto`) aren't resolved at
    boot — kube Services often won't resolve in a plain startup process.
    The allowlist itself is the trust boundary; ITAR mode is the
    kill-switch.
    """
    violations: list[str] = []

    bypass_on = is_bypass_configured()

    if _itar_blocks_bypass() and bypass_on:
        violations.append(
            "ODIN_ITAR_MODE=1 is set AND insecure-broker bypass is configured "
            "(ODIN_ALLOW_INSECURE_BAMBU_BROKER + ODIN_BAMBU_INSECURE_BROKER_HOSTS). "
            "Air-gap deployments must not enable plaintext MQTT to demo brokers."
        )

    if "ODIN_BAMBU_INSECURE_BROKER_PORT" in os.environ:
        try:
            _read_insecure_port()
        except ValueError as e:
            violations.append(str(e))

    for host in _read_allowlist():
        # If the entry is a literal IP, refuse non-loopback/non-RFC1918.
        # Hostnames are passed through (the allowlist *is* the trust
        # boundary).
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            continue
        if not (
            addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_unspecified
        ):
            violations.append(
                f"ODIN_BAMBU_INSECURE_BROKER_HOSTS contains public IP {host!r} — "
                "allowlist may only contain hostnames or RFC1918/loopback literals."
            )

    return violations


def enforce_boot_audit() -> None:
    """Raise RuntimeError if the env config is invalid. No-op on clean
    config. Called from `core/app.py` startup alongside the existing
    `enforce_boot_config` ITAR audit."""
    violations = audit_boot_config()
    if violations:
        msg = (
            "Bambu broker policy boot audit failed:\n"
            + "\n".join(f"  - {v}" for v in violations)
            + "\n\nFix the env vars or remove the bypass entirely."
        )
        log.critical(msg)
        raise RuntimeError(msg)
    if is_bypass_configured():
        # Healthy bypass — log it once at boot so the green-deploy line
        # documents the surface area.
        log.warning(
            "bambu_broker_insecure_bypass_enabled hosts=%s port=%d "
            "(intended for demo/CI compose stacks only)",
            ",".join(_read_allowlist()), _read_insecure_port(),
        )
