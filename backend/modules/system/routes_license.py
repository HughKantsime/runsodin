"""License refresh routes: "Refresh license" and the portal's last refresh answer.

Split from routes_health.py (route file size limit). A cancelled or timed-out
license POST may still complete at the portal: the synchronous outbound worker
cannot be interrupted. Reconcile portal state before retrying a mutation.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from core.outbound_http import pinned_async_request
from core.db import get_db
from core.dependencies import log_audit
from core.rbac import require_superadmin
from core.config import settings
from license_manager import (
    get_license, verify_license_text, get_installation_id,
    get_device_keypair, sign_license_challenge, require_license_mutation_enabled, install_portal_license,
    is_expired_replacement,
)
from modules.system.license_refresh import (
    FILE_REQUIRED_STATUSES, earlier_end_date, ends_within_clock_allowance, read_last_refresh, record_last_refresh, refresh_status_from, server_end_date,
)
from modules.system.routes_health import _fetch_license_challenge

log = logging.getLogger("odin.api")
router = APIRouter()


@router.get("/license/last-refresh", tags=["License"])
def get_license_last_refresh(current_user: dict = Depends(require_superadmin(scope="read"))):
    """What the license portal said at the last "Refresh license" ({status, at, expires_at}).

    Superadmin only: it shows billing state (canceled, no longer active). Null
    until a refresh has happened for the installed license file.
    """
    license_info = get_license(persist_clock=False)
    return {"last_refresh": read_last_refresh(license_info.license_sha256),
            "license_sha256": license_info.license_sha256}


@router.post("/license/reactivate", tags=["License"])
async def reactivate_license(
    current_user: dict = Depends(require_superadmin(scope="write")),
    db: Session = Depends(get_db),
    license_mutation_allowed: None = Depends(require_license_mutation_enabled),
):
    """Refresh the license from the portal ("Refresh license" in Settings > License). Admin only.

    The portal answers with ``refresh_status``:

    * ``renewed``: a new signed file is installed.
    * ``not_renewed_yet``: no new period yet; the current file stays.
    * ``canceled``: an end date is reported; a replacement file that ends
      earlier is installed when the portal sends one.
    * ``not_entitled``: the portal sends a signed replacement that has already
      ended. It is installed and persisted, so the server stays on Community
      across restarts. A later refresh installs a valid file again if
      entitlement returns.

    A portal that sends no ``refresh_status`` is treated as ``renewed``. The
    answer is stored and returned to superadmins by ``GET /license/last-refresh``
    until the next refresh or until another license file is installed.
    """
    license_server_url = settings.license_server_url

    current_license = get_license()
    if not current_license.key:
        raise HTTPException(status_code=400, detail="Current license has no key. Cannot reactivate.")

    installation_id = get_installation_id()

    # S1: fetch challenge, sign with device key.
    nonce = await _fetch_license_challenge(license_server_url, installation_id)
    signature = sign_license_challenge("reactivate", current_license.key, installation_id, nonce)

    # v1.8.9 (codex pass 13): DNS-pinned, no env proxy.
    try:
        resp = await pinned_async_request(
            "POST", f"{license_server_url}/api/v1/reactivate", timeout=15,
            json={
                "key": current_license.key,
                **({"paid_binding": "device-v1", "device_pubkey": get_device_keypair()[1]} if current_license.device_bound else {}),
                "installation_id": installation_id,
                "nonce": nonce,
                "signature": signature,
                # Unsigned hint (B.4): lets the portal tell "renewed" from "not renewed yet"
                # by comparing with the file this server holds.
                **({"current_expires_at": current_license.expires_at} if current_license.expires_at else {}),
            },
        )
    except Exception as e:
        log.error("License reactivation failed — could not reach license server: %s", e)
        raise HTTPException(status_code=502, detail="Could not reach license server.")

    from .license_portal import activation_response
    data = activation_response(resp, "Reactivation failed")
    license_content = data.get("license_file")
    refresh_status = refresh_status_from(data)
    if refresh_status is None:
        raise HTTPException(status_code=502, detail="The license portal sent an answer this version of O.D.I.N. does not understand. Your installed license is unchanged. Update O.D.I.N. or contact support.")
    if not license_content and refresh_status in FILE_REQUIRED_STATUSES:
        raise HTTPException(status_code=502, detail="License server returned no license file")

    def audit_refresh(candidate):
        from core.license_lock import enabled, reconcile
        if enabled(): reconcile(db)
        log_audit(db, "license.reactivated", "system", details={"tier": candidate.tier, "licensee": candidate.licensee})
        db.commit()

    from license_manager import license_file_lock
    with license_file_lock():
        if license_content:
            candidate = verify_license_text(license_content.strip()) if isinstance(license_content, str) else None
            if candidate is None:
                raise HTTPException(status_code=502, detail="The license portal sent a license file that could not be read. Your installed license is unchanged; contact support.")
            # Every refresh is for the requested key, regardless of status or expiry.
            if candidate.key != current_license.key:
                raise HTTPException(status_code=400, detail="License server returned a license for a different key than the one installed. Your installed license is unchanged.")
            # The answer and the file must agree: an ended file only with not_entitled,
            # and not_entitled only with a file that has ended (or ends within the clock
            # allowance: the portal signs it to end at once, and this server's clock may
            # run a little behind). Anything else changes nothing.
            ended = is_expired_replacement(candidate)
            if refresh_status == "not_entitled" and not (ended or ends_within_clock_allowance(candidate)):
                raise HTTPException(status_code=502, detail="The license portal said this license is no longer active but sent a license that has not ended. Your installed license is unchanged; contact support.")
            if refresh_status != "not_entitled" and not candidate.valid and ended:
                raise HTTPException(status_code=502, detail="The license portal sent a license that has already ended with an answer that does not say so. Your installed license is unchanged; contact support.")
            if current_license.device_bound:
                if candidate is None or not (candidate.valid or ended) or not candidate.device_bound or candidate.trial:
                    raise HTTPException(status_code=502, detail="The portal returned an invalid device-bound refresh. Your installed license is unchanged; contact support.")
            license_info = install_portal_license(license_content, after_install=audit_refresh,
                                                 allow_expired=refresh_status == "not_entitled",
                                                 expected_key=current_license.key,
                                                 expected_sha256=current_license.license_sha256)
            expires_at = license_info.expires_at
        else:
            # not_renewed_yet / canceled with no new file: the installed file stays.
            from license_manager import load_license
            license_info = load_license()  # Read disk: another worker's cache may still hold A.
            if (not license_info.license_sha256
                    or license_info.license_sha256 != current_license.license_sha256):
                raise HTTPException(status_code=409, detail="The installed license changed during refresh. Refresh again for the current license.")
            log_audit(db, "license.refresh_checked", "system", details={"refresh_status": refresh_status})
            db.commit()
            # Pro stops when the installed file ends, whatever later date the portal states.
            expires_at = earlier_end_date(server_end_date(data), license_info.expires_at)

        last_refresh = record_last_refresh(db, refresh_status, expires_at or None, license_info.license_sha256)
    return {
        "status": "reactivated",
        "refresh_status": refresh_status,
        "tier": license_info.tier if license_info.valid else "community",
        "valid": license_info.valid,
        "licensee": license_info.licensee,
        "expires_at": expires_at,
        "last_refresh": last_refresh,
    }
