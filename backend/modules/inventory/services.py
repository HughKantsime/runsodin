"""Inventory services — sanctioned cross-module surface for spool + Spoolman helpers.

Route handlers under modules/inventory/routes/ expose HTTP endpoints.
Anything that needs to be called from OUTSIDE the inventory module
(e.g. jobs_lifecycle.py pushing consumption back to Spoolman on job
completion) lives here, in a services.py that other modules can import
via the `.services import` allowlist entry in
tests/test_contracts/test_no_cross_module_imports.py.
"""

from __future__ import annotations

import logging

from core.config import settings
from core.redaction import redact_urls
from core.webhook_utils import _pin_dns, WebhookSSRFError

log = logging.getLogger("odin.api")



def _post_spoolman_usage(base: str, spool_id: int, grams: float):
    """Direct, DNS-pinned administrator-configured integration. No redirects/proxies.

    Spoolman declares the usage endpoint as ``PUT /api/v1/spool/{id}/use``
    with body ``{"use_weight": grams}`` (the same verb archive delivery used);
    any other method is answered 405. Hardware acceptance: confirm against
    the deployed Spoolman version (HARDWARE_ACCEPTANCE_CHECKLIST).
    """
    import ipaddress
    import socket
    from urllib.parse import urlsplit
    import httpx
    from core.itar import is_itar_mode

    if isinstance(spool_id, bool) or not isinstance(spool_id, int) or spool_id <= 0:
        raise ValueError("Invalid Spoolman spool ID")
    parsed = urlsplit(base)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or
            parsed.username is not None or parsed.password is not None or
            parsed.query or parsed.fragment):
        raise ValueError("Invalid configured Spoolman base URL")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    answers = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    addresses = []
    for answer in answers:
        address = ipaddress.ip_address(answer[4][0])
        checked = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped else address
        if (checked.is_link_local or checked.is_unspecified or checked.is_multicast or
                (checked.is_reserved and not checked.is_loopback) or str(checked) in {"100.100.100.200", "fd00:ec2::254"}):
            raise WebhookSSRFError("Configured Spoolman destination is forbidden")
        if is_itar_mode() and not (checked.is_private or checked.is_loopback):
            raise WebhookSSRFError("Configured Spoolman destination is public in ITAR mode")
        addresses.append(str(address))
    if not addresses:
        raise WebhookSSRFError("Configured Spoolman destination has no addresses")
    url = base.rstrip('/') + f"/api/v1/spool/{spool_id}/use"
    with _pin_dns(parsed.hostname, port, addresses):
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=5) as client:
            return client.put(url, json={"use_weight": grams})

def push_consumption_to_spoolman(deductions: list) -> list:
    """PUT usage records to the configured Spoolman instance.

    Ships bidirectional sync (v1.8.5): ODIN already owns the authoritative
    local spool count; this pushes the consumption delta back to Spoolman
    so its inventory stays in lock-step.

    Args:
        deductions: list of dicts with at least:
            - spoolman_spool_id: int | None  (None = spool not linked to Spoolman; skip)
            - grams: float  (positive consumption amount)
            - job_id: int   (for log context)

    Returns:
        list[str] of human-readable error messages, one per failed push.
        Empty list means every linked deduction was accepted.

    Design notes:
        * Fails loud — errors are returned to the caller, which surfaces
          them via log + job.notes. No silent swallowing.
        * Uses a fixed administrator-configured integration destination, with
          DNS pinning, metadata exclusions and ITAR checks. Local Spoolman
          is supported; generic webhook policy remains unchanged.
        * Skips entries with spoolman_spool_id=None without raising.
          A job may involve spools that aren't linked to Spoolman; that's
          a supported state.
        * No-op when settings.spoolman_url is empty — Spoolman integration
          is disabled.
    """
    errors: list = []

    base = (settings.spoolman_url or "").rstrip("/")
    if not base:
        return errors  # integration disabled; not an error

    for d in deductions:
        spoolman_id = d.get("spoolman_spool_id")
        if not spoolman_id:
            continue  # not linked — skip silently

        grams = float(d.get("grams", 0) or 0)
        if grams <= 0:
            continue  # nothing consumed (edge case)

        url = f"{base}/api/v1/spool/{spoolman_id}/use"
        try:
            resp = _post_spoolman_usage(base, spoolman_id, grams)
            # Spoolman returns 200 on success. 4xx is usually a bad spool
            # id or a malformed body — surface it so operators see it.
            if hasattr(resp, "status_code") and resp.status_code >= 300:
                errors.append(
                    f"Spoolman spool={spoolman_id} job={d.get('job_id')} "
                    f"rejected push: HTTP {resp.status_code}"
                )
        except WebhookSSRFError as e:
            errors.append(
                f"Spoolman URL blocked by SSRF check (spool={spoolman_id}): {redact_urls(e)}"
            )
        except Exception as e:
            errors.append(
                f"Spoolman push failed (spool={spoolman_id} job={d.get('job_id')}): {redact_urls(e)}"
            )

    return errors


def deliver_spoolman_usage(spoolman_spool_id: int, grams: float):
    """Deliver one usage record; return (state, reason) for a durable outbox row.

    States: 'sent' (Spoolman answered 2xx), 'failed' (refused: HTTP error,
    invalid identity/URL, SSRF/ITAR block), 'pending' (not delivered and safe
    to retry: integration disabled or the connection was never established),
    'outcome_unknown' (the request may have reached Spoolman; never resent,
    which would double-deduct).
    """
    import httpx
    base = (settings.spoolman_url or "").rstrip("/")
    if not base:
        return "pending", "spoolman_not_configured"
    try:
        resp = _post_spoolman_usage(base, spoolman_spool_id, grams)
    except (ValueError, WebhookSSRFError) as exc:
        return "failed", f"refused: {redact_urls(exc)}"[:500]
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        return "pending", f"connect_failed: {type(exc).__name__}"
    except Exception as exc:
        return "outcome_unknown", f"transport: {type(exc).__name__}"
    status = getattr(resp, "status_code", None)
    if isinstance(status, int) and 200 <= status < 300:
        return "sent", None
    return "failed", f"HTTP {status}"
