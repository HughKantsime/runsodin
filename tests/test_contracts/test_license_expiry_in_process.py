"""Review MEDIUM 4: a license that expires while the process runs loses its features.

Real signed license (tier_license fixture), real get_license cache and the real
require_feature / feature_gate path; only wall-clock time passes.
"""
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient


def test_cached_license_expires_mid_process_without_restart(tier_license):
    import license_manager as lm
    expires = (datetime.now(timezone.utc) + timedelta(seconds=1.5)).isoformat()
    tier_license("pro", expires_at=expires)
    app = FastAPI()

    @app.post("/orders", dependencies=[Depends(lm.feature_gate("orders", keep_existing_records=True))])
    def create():
        return {"ok": True}

    client = TestClient(app)
    assert lm.get_license().has_feature("orders") and client.post("/orders").status_code == 200
    cached = lm.get_license()
    time.sleep(2.0)  # the file is untouched: only the expiry instant passes
    info = lm.get_license()
    assert info is not cached
    assert info.valid is False and info.expired is True and info.max_users == 1
    assert not info.has_feature("orders")
    r = client.post("/orders")
    assert r.status_code == 403 and "Current tier: pro" in r.json()["detail"]


def test_unexpired_license_keeps_the_cache(tier_license):
    import license_manager as lm
    tier_license("pro")
    first = lm.get_license()
    assert lm.get_license() is first
