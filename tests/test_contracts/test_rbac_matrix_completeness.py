"""Completeness of server-side permission-matrix enforcement (spec permissions-enforcement.md).

Reads the live route table of the real app (create_app() in a cert-G harness
subprocess) and checks, by walking each route's dependant tree (not source
text), that:
* every action key in core.rbac_policy maps to at least one route
  (core.rbac_routes.ACTION_ROUTES), and every mapped route exists;
* every mapped route carries core.rbac.require_action for its key, and every
  page route carries core.rbac.require_page for its page;
* every write route under /api is mapped to a key or has a documented
  exemption (core.rbac_routes.write_exemption);
* every page key either guards page-only endpoints or documents why its data
  comes from shared endpoints;
* the Settings > Permissions editor lists every key the server enforces.
"""
import re
from pathlib import Path

import pytest

from tests.test_contracts.test_cert_G_harness import run_probe

ROOT = Path(__file__).resolve().parents[2]

PROBE = r'''
from fastapi.routing import APIRoute
def marks(dependant, out):
    for sub in dependant.dependencies:
        for attr in ("__odin_rbac_action__", "__odin_rbac_page__"):
            value = getattr(sub.call, attr, None)
            if value is not None:
                out.append([attr, value])
        marks(sub, out)
rows = []
for route in app.routes:
    if isinstance(route, APIRoute):
        found = []
        marks(route.dependant, found)
        for method in sorted(route.methods):
            rows.append({"method": method, "path": route.path, "marks": found})
OUT["routes"] = rows
'''


@pytest.fixture(scope="module")
def routes(tmp_path_factory):
    return run_probe("rbac-completeness", PROBE, tmp_path_factory.mktemp("rbac-completeness"))["routes"]


@pytest.fixture(scope="module")
def table(routes):
    return {(r["method"], r["path"]): r["marks"] for r in routes}


from core.rbac_policy import RBAC_DEFAULT_ACTION_ACCESS, RBAC_DEFAULT_PAGE_ACCESS  # noqa: E402
from core.rbac_routes import ACTION_FALLBACKS, ACTION_ROUTES, PAGE_ROUTES, PAGE_SHARED, write_exemption  # noqa: E402

WRITE = {"POST", "PUT", "PATCH", "DELETE"}


def test_every_action_key_is_mapped_to_routes():
    assert sorted(ACTION_ROUTES) == sorted(RBAC_DEFAULT_ACTION_ACCESS)
    assert all(ACTION_ROUTES[key] for key in ACTION_ROUTES)


def test_every_page_key_guards_page_routes_or_documents_shared_data():
    assert set(PAGE_ROUTES) | set(PAGE_SHARED) == set(RBAC_DEFAULT_PAGE_ACCESS)
    assert not set(PAGE_ROUTES) & set(PAGE_SHARED)


@pytest.mark.parametrize("key", sorted(ACTION_ROUTES))
def test_mapped_routes_exist_and_declare_require_action(table, key):
    for method, path in ACTION_ROUTES[key]:
        assert (method, path) in table, (key, method, path)
        actions = [value for attr, value in table[(method, path)] if attr == "__odin_rbac_action__"]
        assert actions and actions[0] == [key, ACTION_FALLBACKS.get((method, path))], (key, method, path, table[(method, path)])
        # the /api/v1 alias is the same handler and is enforced the same way
        alias = (method, "/api/v1" + path[len("/api"):])
        if alias in table:
            assert table[alias] == table[(method, path)], alias


@pytest.mark.parametrize("page", sorted(PAGE_ROUTES))
def test_page_routes_declare_require_page(table, page):
    for method, path in PAGE_ROUTES[page]:
        assert ["__odin_rbac_page__", page] in table[(method, path)], (page, method, path)


def test_no_route_declares_a_key_it_is_not_mapped_to(table):
    mapped = {(m, p): key for key, rs in ACTION_ROUTES.items() for m, p in rs}
    for (method, path), marks in table.items():
        if not path.startswith("/api/") or path.startswith("/api/v1/"):
            continue
        for attr, value in marks:
            if attr == "__odin_rbac_action__":
                assert mapped.get((method, path)) == value[0], (method, path, value)


def test_every_write_route_has_a_key_or_a_documented_exemption(table):
    mapped = {(m, p) for rs in ACTION_ROUTES.values() for m, p in rs}
    missing = sorted((m, p) for (m, p) in table if m in WRITE and p.startswith("/api/") and not p.startswith("/api/v1/")
                     and (m, p) not in mapped and not write_exemption(m, p))
    assert missing == []


def test_exemptions_do_not_shadow_mapped_routes():
    for key, rs in ACTION_ROUTES.items():
        for method, path in rs:
            assert write_exemption(method, path) is None, (key, method, path)


def test_permissions_editor_lists_every_enforced_key():
    source = (ROOT / "frontend/src/pages/admin/Permissions.tsx").read_text()
    groups = re.search(r"const PAGE_GROUPS = \[(.*?)\n\]", source, re.S).group(1)
    actions = re.search(r"const ACTION_GROUPS = \[(.*?)\n\]", source, re.S).group(1)
    assert set(re.findall(r"'([a-z_]+)'", re.sub(r"label: '[^']*'", "", groups))) == set(RBAC_DEFAULT_PAGE_ACCESS)
    assert set(re.findall(r"'([a-z_]+\.[a-z_]+)'", actions)) == set(RBAC_DEFAULT_ACTION_ACCESS)
