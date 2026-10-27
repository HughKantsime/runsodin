"""Cert F / cluster B — producer/consumer drift guard for the UI cert tests.

The frontend cert tests (frontend/src/components/**/{APITokenManager.crud,SessionManager.devices,GroupManager.groups,
OrgManager.access}.cert.test.tsx) embed bodies captured from the real backend. This test compares the field sets of
each embedded fixture with the bodies the LiveOdin tests captured in the same run
(artifacts/application-certification/cert-F/B-wire-captures.json). Run it after the other test_cert_F_B_* files.
"""
import json
import re
from pathlib import Path

import pytest
from tests.test_contracts.test_spool_slot_prevention import graph, review_db, USER

ROOT = Path(__file__).resolve().parents[2]
CAPTURE = ROOT / "artifacts" / "application-certification" / "cert-F" / "B-wire-captures.json"
FILES = [
    "frontend/src/components/auth/APITokenManager.crud.cert.test.tsx",
    "frontend/src/components/auth/SessionManager.devices.cert.test.tsx",
    "frontend/src/components/admin/GroupManager.groups.cert.test.tsx",
    "frontend/src/components/admin/OrgManager.access.cert.test.tsx",
]


@pytest.mark.parametrize("path", FILES)
def test_embedded_wire_fixture_matches_live_producer_fields(path):
    captured = json.loads(CAPTURE.read_text())
    src = (ROOT / path).read_text()
    m = re.search(r"const WIRE: any = (\{.*?\})\n// WIRE-END", src, re.S)
    assert m, path
    embedded = json.loads(m.group(1))
    for key, value in embedded.items():
        assert key in captured, key
        live = captured[key]
        assert type(value) is type(live), key
        if isinstance(value, list):
            assert value and live, key
            assert sorted(value[0]) == sorted(live[0]), (key, set(value[0]) ^ set(live[0]))
        else:
            assert sorted(value) == sorted(live), (key, set(value) ^ set(live))


def test_embedded_printer_wire_matches_current_route_without_capture_dependency(graph):
    """The printer fixture also has a self-contained live producer check."""
    from modules.printers.routes_crud import list_printers
    from modules.printers.schemas import PrinterResponse
    from sqlalchemy import text
    graph.execute(text("UPDATE printers SET shared=FALSE,tags='[]',timelapse_enabled=FALSE WHERE id=9"))
    graph.commit()
    rows = list_printers(current_user=USER, db=graph)
    live = PrinterResponse.model_validate(next(row for row in rows if row.id == 9)).model_dump(mode='json')
    source = (ROOT / FILES[-1]).read_text()
    embedded = json.loads(re.search(r"const WIRE: any = (\{.*?\})\n// WIRE-END", source, re.S)[1])
    for printer in embedded['printers_list']:
        assert sorted(printer) == sorted(live)
        assert printer['installed_nozzles'] == live['installed_nozzles']
        assert printer['maintenance'] == live['maintenance']
