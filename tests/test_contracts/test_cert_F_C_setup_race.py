"""Security review MED 3: concurrent first-run POST /api/setup/admin calls created several admins.
Real server (LiveOdin, SQLite); the PostgreSQL case is in ops/database_parity/run_certf_fixes.py."""
from __future__ import annotations

import secrets
import threading

import requests

from tests.test_contracts.test_cert_F_00_live import LiveOdin, strong_password


def test_concurrent_setup_admin_creates_exactly_one_admin(tmp_path):
    with LiveOdin(tmp_path / "odin") as o:
        barrier = threading.Barrier(8)
        codes = []

        def claim(i):
            body = {"username": f"racer{i}", "email": f"racer{i}@example.test", "password": strong_password()}
            barrier.wait()
            codes.append(requests.post(o.url + "/api/setup/admin", json=body, timeout=60).status_code)

        threads = [threading.Thread(target=claim, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(codes).count(200) == 1, codes
        assert all(c in (200, 403) for c in codes), codes
        assert o.sql("SELECT COUNT(*) FROM users")[0][0] == 1
        assert o.sql("SELECT COUNT(*) FROM users WHERE role='admin'")[0][0] == 1
        # Later claims stay refused.
        late = requests.post(o.url + "/api/setup/admin", json={"username": "late", "email": "late@example.test",
                                                               "password": strong_password()}, timeout=30)
        assert late.status_code == 403
