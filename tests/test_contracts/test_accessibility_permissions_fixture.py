"""The accessibility audit serves the server's default permission matrix.

2.0 pages enforce every page key (RoleGate), so the audit's /api/permissions fixture
must be the real default matrix, or personas are redirected away from the pages under
audit. This keeps tests/accessibility/default_permissions.json equal to core.rbac_policy."""
import json
from pathlib import Path

from core.rbac_policy import RBAC_DEFAULT_ACTION_ACCESS, RBAC_DEFAULT_PAGE_ACCESS

FIXTURE = Path(__file__).resolve().parents[1] / "accessibility" / "default_permissions.json"


def test_accessibility_permissions_fixture_is_the_default_matrix():
    assert json.loads(FIXTURE.read_text()) == {
        "page_access": RBAC_DEFAULT_PAGE_ACCESS, "action_access": RBAC_DEFAULT_ACTION_ACCESS}


def test_audited_personas_hold_the_pages_they_audit():
    audit = (FIXTURE.parent / "accessibility_audit.mjs").read_text()
    for route, page, persona in (("/printers", "printers", "operator"), ("/printers", "printers", "viewer"),
                                 ("/jobs", "jobs", "viewer"), ("/upload", "upload", "operator"),
                                 ("/education-reports", "education_reports", "operator")):
        assert persona in RBAC_DEFAULT_PAGE_ACCESS[page], (route, persona)
    assert "id: 'A05', route: '/upload', fixture: 'jobs.json', personas: ['operator']" in audit
