"""Cert F / cluster B: deleting a library filament that spools still use answers 409, never 500 (real server)."""
from __future__ import annotations

import pytest

from tests.test_contracts.test_cert_F_B_00_support import LiveOdinB


@pytest.fixture(scope="module")
def odin(tmp_path_factory):
    o = LiveOdinB(tmp_path_factory.mktemp("filament-delete")).start()
    o.boot_admin()
    o.op = o.mkuser("fil_operator", "operator")
    yield o
    o.stop()


def _filament(o, name):
    r = o.post("/api/filaments", json={"brand": "Cert", "name": name, "material": "PLA", "color_hex": "112233"},
               headers=o.op["headers"])
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_filament_used_by_a_spool_is_refused_with_409_and_kept(odin):
    fid = _filament(odin, "In use")
    spool = odin.post("/api/spools", json={"filament_id": fid}, headers=odin.op["headers"])
    assert spool.status_code in (200, 201), spool.text
    r = odin.delete(f"/api/filaments/lib_{fid}", headers=odin.op["headers"])
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "Filament is in use by spools and cannot be deleted."
    assert odin.one("SELECT 1 FROM filament_library WHERE id=:i", i=fid) is not None
    assert odin.one("SELECT filament_id FROM spools WHERE id=:i", i=spool.json()["id"]).filament_id == fid


def test_unused_filament_is_deleted(odin):
    fid = _filament(odin, "Unused")
    assert odin.delete(f"/api/filaments/{fid}", headers=odin.op["headers"]).status_code == 204
    assert odin.one("SELECT 1 FROM filament_library WHERE id=:i", i=fid) is None
