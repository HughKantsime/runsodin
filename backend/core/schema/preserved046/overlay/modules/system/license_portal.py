"""Portal response contract shared by ODIN paid activation flows."""
from fastapi import HTTPException


def activation_response(resp, failure_message="Activation failed"):
    # Preserve the pre-binding status and text for non-JSON portal refusals.
    if resp.status_code != 200 and not resp.headers.get("content-type", "").startswith("application/json"):
        raise HTTPException(status_code=resp.status_code, detail=f"License server returned {resp.status_code}")
    try:
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError()
    except (ValueError, TypeError):
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=f"License server returned {resp.status_code}")
        raise HTTPException(status_code=502, detail="License server returned an unreadable response. Your installed license is unchanged.")
    if resp.status_code != 200:
        messages = {
            "DEVICE_MISMATCH": "Device key mismatch. Restore the original installation ID and device key, or contact support for an authorized re-issue.",
            "PROOF_REQUIRED": "A fresh proof is required. Retry with a new challenge or use offline activation.",
            "PROOF_FAILED": "Device proof failed. Retry with a fresh challenge; contact support if the key was lost.",
            "CAPACITY_EXCEEDED": "No installation grants remain. Unactivate the old installation or contact support.",
            "LEGACY_REBIND_REQUIRED": "This activation needs administrator recovery on the portal. Contact support.",
        }
        code = data.get("code") if isinstance(data.get("code"), str) else None
        status = resp.status_code
        if code is None:  # Older portals return only their existing error text.
            raise HTTPException(status_code=status, detail=data.get("error") if isinstance(data.get("error"), str) else failure_message)
        raise HTTPException(status_code=status, detail={"code": code, "message": messages.get(code, "The portal refused this request. Contact support with the error code. Your installed license is unchanged.")})
    return data
