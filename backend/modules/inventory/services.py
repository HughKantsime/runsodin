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
from core.webhook_utils import safe_post, WebhookSSRFError

log = logging.getLogger("odin.api")


def push_consumption_to_spoolman(deductions: list) -> list:
    """POST usage records to the configured Spoolman instance.

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
        * Uses safe_post() so the Spoolman URL (user-configured) gets the
          SSRF DNS-pin treatment. Rebinding a Spoolman hostname to an
          internal target is the attack we guard against.
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
            resp = safe_post(
                url,
                json={"use_weight": grams},
                timeout=5,
            )
            # Spoolman returns 200 on success. 4xx is usually a bad spool
            # id or a malformed body — surface it so operators see it.
            if hasattr(resp, "status_code") and resp.status_code >= 400:
                errors.append(
                    f"Spoolman spool={spoolman_id} job={d.get('job_id')} "
                    f"rejected push: HTTP {resp.status_code}"
                )
        except WebhookSSRFError as e:
            errors.append(
                f"Spoolman URL blocked by SSRF check (spool={spoolman_id}): {e}"
            )
        except Exception as e:
            errors.append(
                f"Spoolman push failed (spool={spoolman_id} job={d.get('job_id')}): {e}"
            )

    return errors
