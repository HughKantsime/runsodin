"""
Contract test — Printer control + smart-plug routes must enforce org scoping.

Guards R1 from the 2026-04-12 Codex adversarial review:
    routes_controls.py and routes_smart_plug.py loaded Printer rows by ID
    and issued commands with no check on printer.org_id. An operator from
    Org A could stop prints, power-cycle printers, reconfigure smart-plug
    integrations, and change print speed/fans/lights on Org B's printers.

Source-level gate: every handler that loads a printer by ID and acts on
it must call check_org_access(current_user, printer.org_id) before the
action, and return 404 (not 403) on mismatch to avoid disclosing resource
existence across tenants.

Run without container: pytest tests/test_contracts/test_printer_controls_org_scope.py -v
"""

import ast
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
ROUTES_CONTROLS = BACKEND_DIR / "modules" / "printers" / "routes_controls.py"
ROUTES_SMART_PLUG = BACKEND_DIR / "modules" / "printers" / "routes_smart_plug.py"

# Every handler in these files that takes a printer_id should gate on org.
# The audit found 9 + 5 = 14 such handlers. We allow some wiggle room in
# case future refactors inline things, but we assert a minimum.
CONTROLS_MIN_CHECKS = 9
SMART_PLUG_MIN_CHECKS = 5


class TestPrinterControlsOrgScope:
    """routes_controls.py handlers must enforce org access."""

    def test_imports_check_org_access(self):
        source = ROUTES_CONTROLS.read_text()
        assert "check_org_access" in source, (
            "routes_controls.py does not import check_org_access. "
            "R1 from the 2026-04-12 review requires every printer-control "
            "handler to validate the caller's org against printer.org_id."
        )

    def test_every_handler_calls_check_org_access(self):
        source = ROUTES_CONTROLS.read_text()
        count = source.count("check_org_access(current_user, printer.org_id)")
        assert count >= CONTROLS_MIN_CHECKS, (
            f"Found only {count} check_org_access calls in routes_controls.py; "
            f"expected at least {CONTROLS_MIN_CHECKS}. Every handler that loads "
            f"a Printer row must gate on org_id. This is R1 from the adversarial "
            f"review — operators in one org should not be able to control printers "
            f"belonging to another."
        )

    def test_cross_org_returns_404_not_403(self):
        """The convention is to mask existence — use 404 for RBAC cross-tenant failures."""
        source = ROUTES_CONTROLS.read_text()
        tree = ast.parse(source)
        # Find every check_org_access call followed by an HTTPException
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                # Look for `if not check_org_access(...): raise HTTPException(status_code=XXX, ...)`
                test = ast.unparse(node.test) if hasattr(ast, "unparse") else ""
                if "check_org_access" in test and "not " in test:
                    for stmt in node.body:
                        if isinstance(stmt, ast.Raise) and ast.unparse(stmt):
                            raise_src = ast.unparse(stmt)
                            if "status_code=403" in raise_src:
                                violations.append(raise_src)
        assert not violations, (
            f"check_org_access failures in routes_controls.py return 403 "
            f"instead of 404, leaking cross-tenant resource existence:\n"
            + "\n".join(f"  {v}" for v in violations)
        )


class TestSmartPlugOrgScope:
    """routes_smart_plug.py must enforce org access on plug config + power control."""

    def test_imports_check_org_access(self):
        source = ROUTES_SMART_PLUG.read_text()
        assert "check_org_access" in source, (
            "routes_smart_plug.py does not import check_org_access. "
            "R1 from the 2026-04-12 review requires every plug handler to "
            "validate printer ownership before acting — otherwise an operator "
            "can repoint the smart-plug integration to attacker-controlled "
            "infrastructure or power-cycle other tenants' printers."
        )

    def test_has_org_access_helper(self):
        """The helper exists so each handler's one-liner can't be silently removed."""
        source = ROUTES_SMART_PLUG.read_text()
        assert "_require_printer_org_access" in source, (
            "routes_smart_plug.py should define a _require_printer_org_access "
            "helper so the tenancy gate is one call per handler, hard to miss "
            "in review."
        )

    def test_every_write_or_power_handler_calls_gate(self):
        """PUT /plug, DELETE /plug, POST /plug/on, /plug/off, /plug/toggle must gate."""
        source = ROUTES_SMART_PLUG.read_text()
        count = source.count("_require_printer_org_access(printer_id, current_user, db)")
        assert count >= SMART_PLUG_MIN_CHECKS, (
            f"Found only {count} _require_printer_org_access calls in "
            f"routes_smart_plug.py; expected at least {SMART_PLUG_MIN_CHECKS} "
            f"(update_plug_config, remove_plug_config, plug_power_on, "
            f"plug_power_off, plug_power_toggle)."
        )
