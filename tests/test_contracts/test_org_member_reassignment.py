"""
Contract test — Org-admin user reassignment must not cross org boundaries.

Guards R2 from the 2026-04-12 Codex adversarial review:
    routes.py:156-170 let an org-scoped admin reassign any user account
    into their org with no check on the target user's current group.
    Attack scenarios:
      1. Malicious org admin pulls users from another org into theirs
         (data leakage + denial of service on the victim org).
      2. Malicious org admin reassigns the superadmin account
         (defined as `role=admin AND group_id IS NULL`) into their org,
         stripping system-wide privileges in one UPDATE.

Source-level gate — the mutation step must be preceded by checks on
the target user's current state. These checks cannot be silently removed.

Run without container: pytest tests/test_contracts/test_org_member_reassignment.py -v
"""

import ast
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
ROUTES = BACKEND_DIR / "modules" / "organizations" / "routes.py"


def _get_function_source(source: str, name: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node)
    return ""


class TestOrgMemberReassignment:
    """add_org_member() must guard against cross-tenant user hijacking."""

    def test_loads_target_user_before_mutation(self):
        source = ROUTES.read_text()
        fn_src = _get_function_source(source, "add_org_member")
        assert fn_src, "add_org_member function is missing"

        # Target user must be loaded (a SELECT before the UPDATE)
        select_idx = fn_src.find("SELECT id, role, group_id FROM users WHERE id = :uid")
        update_idx = fn_src.find("UPDATE users SET group_id")
        assert select_idx > 0, (
            "add_org_member does not SELECT the target user before UPDATE. "
            "Without loading the current state, we can't enforce R2: "
            "no cross-org reassignment, no superadmin demotion."
        )
        assert select_idx < update_idx, (
            "The target-user SELECT must come BEFORE the UPDATE. "
            "Current ordering makes the guard ineffective."
        )

    def test_rejects_reassigning_superadmin(self):
        source = ROUTES.read_text()
        fn_src = _get_function_source(source, "add_org_member")
        # There must be a check for role == 'admin' with no group_id
        assert 'target.role == "admin"' in fn_src or "target.role == 'admin'" in fn_src, (
            "add_org_member must check if target is 'admin' role. "
            "Otherwise a malicious org admin can reassign the platform "
            "superadmin into their tenant and seize system privileges."
        )
        assert "target.group_id is None" in fn_src, (
            "add_org_member must check target.group_id is None for superadmin "
            "detection. Superadmin is defined as admin-role with no group."
        )

    def test_rejects_cross_org_reassignment(self):
        source = ROUTES.read_text()
        fn_src = _get_function_source(source, "add_org_member")
        # Must check target.group_id against caller_org
        assert "target.group_id is not None" in fn_src, (
            "add_org_member does not check if target user already has a "
            "different group_id. Without this, a malicious org admin can "
            "UPDATE users from other orgs into theirs."
        )

    def test_superadmin_caller_bypasses_org_restriction(self):
        """Superadmin (role=admin, group_id IS NULL) is the legitimate user
        for this route — they must still be able to assign any user to any org."""
        source = ROUTES.read_text()
        fn_src = _get_function_source(source, "add_org_member")
        assert "is_superadmin" in fn_src, (
            "add_org_member does not distinguish superadmin from org-scoped admin. "
            "Both need different rules: superadmin can assign any user, "
            "org admin can only touch their own org's members or unassigned users."
        )

    def test_action_is_audited(self):
        source = ROUTES.read_text()
        fn_src = _get_function_source(source, "add_org_member")
        assert "log_audit" in fn_src, (
            "add_org_member must emit an audit log entry. Without one, "
            "a successful cross-tenant move is untraceable."
        )
