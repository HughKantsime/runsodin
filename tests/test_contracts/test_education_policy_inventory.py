from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def test_policy_inventory_is_complete_and_matches_registered_surfaces() -> None:
    from modules.organizations.education_policy_inventory import (
        POLICY_INVENTORY,
        REGISTERED_EXECUTION_SURFACES,
        validate_policy_inventory,
    )

    validate_policy_inventory()
    assert {entry["surface"] for entry in POLICY_INVENTORY} == set(
        REGISTERED_EXECUTION_SURFACES
    )
    assert "printers.routes_smart_plug" in REGISTERED_EXECUTION_SURFACES


def test_policy_inventory_gate_fails_for_new_unclassified_surface(
    tmp_path: Path,
) -> None:
    from modules.organizations.education_policy_inventory import (
        discover_execution_surfaces,
        validate_policy_inventory,
    )

    backend = tmp_path / "backend"
    sink = backend / "modules" / "new_sink.py"
    sink.parent.mkdir(parents=True)
    sink.write_text(
        'db.execute(text("SELECT * FROM jobs WHERE id=:id"), {"id": 1})\n',
        encoding="utf-8",
    )
    discovered = discover_execution_surfaces(backend)
    assert discovered == {"new_sink"}
    with pytest.raises(RuntimeError, match="missing=.*new_sink"):
        validate_policy_inventory(discovered)


def test_central_policy_interface_covers_lifecycle_and_dispatch() -> None:
    from core.interfaces.education_policy import EducationPolicyProvider

    assert EducationPolicyProvider.__abstractmethods__ == {
        "assert_user_tenant_change_allowed",
        "assert_user_hard_delete_allowed",
        "assert_org_hard_delete_allowed",
        "assert_printer_tenant_change_or_delete_allowed",
        "printer_is_currently_entitled",
        "authorize_dispatch",
        "reconcile_dispatch_denial",
    }


def test_websocket_principal_has_explicit_provenance_and_snapshot_identity() -> None:
    source = (BACKEND / "core" / "app.py").read_text(encoding="utf-8")
    issuer = (
        BACKEND / "modules" / "organizations" / "routes_auth.py"
    ).read_text(encoding="utf-8")
    assert 'principal["_auth_kind"] = "websocket_token"' in source
    assert 'principal["_capability_snapshot_id"]' in source
    assert 'payload.get("jti")' in source
    assert '"capability_snapshot_id": capability_snapshot_id(db, current_user)' in issuer
    assert 'principal["_capability_snapshot_id"] = capability_snapshot_id' in source
