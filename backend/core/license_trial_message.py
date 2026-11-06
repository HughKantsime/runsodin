"""Canonical offline trial proof encoding shared by the license facade."""
from typing import Dict, Any

def trial_request_message(request: Dict[str, Any]) -> bytes:
    return "\n".join([
        "odin-trial-request-v1", request["installation_id"], request["device_pubkey"],
        request["email"], request["nonce"], request["issued_at"],
    ]).encode("utf-8")
