"""Producer wire fixture shared by the cert-B backend tests and UI cert tests."""
import json
import os
from pathlib import Path

from sqlalchemy import text

WIRE = Path(__file__).resolve().parents[2] / "frontend/src/test-fixtures/cert-B-wire.json"


def check_wire(key, value):
    """The UI cert tests consume these producer responses; keep the fixture equal to them.

    CERT_B_WIRE_WRITE=1 rewrites the fixture entry from the producer.
    """
    stored = json.loads(WIRE.read_text()) if WIRE.exists() else {}
    if os.environ.get("CERT_B_WIRE_WRITE") == "1":
        stored[key] = value
        WIRE.write_text(json.dumps(stored, indent=1, sort_keys=True) + "\n")
    assert stored.get(key) == value, f"{WIRE.name}:{key} differs from the producer; rerun with CERT_B_WIRE_WRITE=1"


def no_organizations(db):
    """The install has groups but no organizations (single-tenant)."""
    db.execute(text("UPDATE groups SET is_org=0"))
    db.commit()
