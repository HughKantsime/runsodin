"""License lifecycle request bodies, shared with health routes."""
from pydantic import BaseModel as PydanticBaseModel

class LicenseActivateRequest(PydanticBaseModel):
    key: str
    binding_protocol: str | None = None

    # None preserves ordinary legacy refresh; device-v1 must never downgrade.


class TrialRequestBody(PydanticBaseModel):
    email: str
    offline: bool = False


class OfflineActivationRequestBody(PydanticBaseModel):
    key: str
    nonce: str  # issued by GET /api/v1/challenge on the licensing server
    binding_protocol: str | None = None


class LicenseUnactivateRequest(PydanticBaseModel):
    key: str = None  # Optional — if not provided, read from current license


class OfflineUnactivationRequestBody(PydanticBaseModel):
    nonce: str  # issued by GET /api/v1/challenge on the licensing server


