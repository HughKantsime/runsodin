"""F22-22.6-04: every in-app help link targets a page of the Docusaurus docs site.

Reads the real frontend/src/utils/helpLinks.ts map and checks each target against the
docs source tree (odin-site/docs: docusaurus.config.js routeBasePath '/', sidebars.js
doc ids, docs/**/*.md front matter slugs, and heading anchors generated the way
Docusaurus does). ODIN_SITE_DOCS overrides the default sibling checkout location.
When CERT_H_DOCS_BUILD points at a `docusaurus build` output, the rendered page and
anchor id are checked there too.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HELP_LINKS = ROOT / "frontend/src/utils/helpLinks.ts"
SITE_DOCS = Path(os.environ.get("ODIN_SITE_DOCS", ROOT.parent / "odin-site/docs"))
BUILD = os.environ.get("CERT_H_DOCS_BUILD", "")
BASE = "https://docs.runsodin.com"

pytestmark = pytest.mark.skipif(
    not (SITE_DOCS / "sidebars.js").is_file(),
    reason=f"odin-site docs source not found at {SITE_DOCS} (set ODIN_SITE_DOCS)",
)


def _entries() -> dict[str, str]:
    text = HELP_LINKS.read_text(encoding="utf-8")
    entries = dict(re.findall(r"^  '(/[^']*)': \{ url: '([^']+)'", text, flags=re.M))
    # every entry in the map is parsed (guards against a format change hiding entries)
    assert len(entries) == text.count("{ url: '"), "unparsed helpLinks entries"
    return entries


def _front_matter(md: Path) -> dict[str, str]:
    text = md.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}
    block = text[4:text.index("\n---", 4)]
    return dict(re.findall(r"^(\w+):\s*(.+?)\s*$", block, flags=re.M))


def _routes() -> dict[str, Path]:
    """Docs route -> source file, for every doc listed in the sidebar."""
    config = (SITE_DOCS / "docusaurus.config.js").read_text(encoding="utf-8")
    assert "routeBasePath: '/'" in config
    sidebar_ids = set(re.findall(r"'([a-z0-9\-/]+)'", (SITE_DOCS / "sidebars.js").read_text(encoding="utf-8")))
    routes: dict[str, Path] = {}
    for md in sorted((SITE_DOCS / "docs").rglob("*.md*")):
        relative = md.relative_to(SITE_DOCS / "docs").with_suffix("")
        meta = _front_matter(md)
        name = meta.get("id", relative.name)
        doc_id = name if relative.parent == Path(".") else f"{relative.parent.as_posix()}/{name}"
        if doc_id not in sidebar_ids:
            continue
        route = meta.get("slug") or "/" + doc_id
        routes[route.rstrip("/") or "/"] = md
    return routes


def _slug(heading: str) -> str:
    # github-slugger (used by Docusaurus): lowercase, drop punctuation, spaces -> '-'
    text = re.sub(r"`|\*\*|\[|\]\([^)]*\)", "", heading).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _anchors(md: Path) -> set[str]:
    text = re.sub(r"```.*?```", "", md.read_text(encoding="utf-8"), flags=re.S)
    anchors = set()
    for heading in re.findall(r"^#{2,6} (.+?)\s*$", text, flags=re.M):
        explicit = re.search(r"\{#([\w\-]+)\}$", heading)
        anchors.add(explicit.group(1) if explicit else _slug(heading))
    return anchors


def _sections(md: Path) -> dict[str, str]:
    """anchor -> lowercased text of that section (heading + body up to the next heading of any level);
    '' -> the page title, description and H1."""
    text = re.sub(r"```.*?```", "", md.read_text(encoding="utf-8"), flags=re.S)
    meta = _front_matter(md)
    h1 = re.search(r"^# (.+)$", text, flags=re.M)
    sections = {"": " ".join([meta.get("title", ""), meta.get("description", ""), h1.group(1) if h1 else ""]).lower()}
    parts = re.split(r"^(#{2,6} .+?)\s*$", text, flags=re.M)
    for heading, body in zip(parts[1::2], parts[2::2]):
        title = heading.lstrip("#").strip()
        explicit = re.search(r"\{#([\w\-]+)\}$", title)
        sections[explicit.group(1) if explicit else _slug(title)] = (title + "\n" + body).lower()
    return sections


# What each in-app page is about: the linked page title/H1 (or the anchored section) must say so.
TOPICS = {
    "/": "quick start", "/printers": "printer", "/printers/:id": "control", "/jobs": "schedul",
    "/upload": "upload", "/spools": "spool", "/cameras": "camera", "/orders": "order", "/analytics": "analytic",
    "/detections": "detection", "/settings": "settings", "/timeline": "timeline", "/branding": "brand", "/models": "model",
    "/profiles": "profile", "/products": "product", "/consumables": "consumable", "/maintenance": "maintenance",
    "/utilization": "utilization", "/alerts": "alert", "/archives": "archive", "/calculator": "calculator",
    "/audit": "audit log", "/tv": "tv dashboard",
}


ENTRIES = _entries() if HELP_LINKS.exists() else {}


def test_help_links_cover_the_routes_the_app_shows():
    assert len(ENTRIES) == 24 and "/settings" in ENTRIES and "/timeline" in ENTRIES and "/printers/:id" in ENTRIES
    assert "/settings/branding" not in ENTRIES  # the app's branding route is /branding
    assert set(ENTRIES) == set(TOPICS)


@pytest.mark.parametrize("route", sorted(ENTRIES))
def test_help_link_target_is_a_docs_page_and_heading(route):
    url = ENTRIES[route]
    assert url.startswith(BASE + "/"), url
    path, _, anchor = url[len(BASE):].partition("#")
    key = path.rstrip("/") or "/"
    routes = _routes()
    assert key in routes, f"{route} -> {url}: no docs page at {path}"
    if anchor:
        assert anchor in _anchors(routes[key]), f"{route} -> {url}: no heading #{anchor}"
    # topic relevance: the anchored section, or the page title/description/H1, names the topic
    section = _sections(routes[key])[anchor]
    assert TOPICS[route] in section, f"{route} -> {url}: target does not cover '{TOPICS[route]}'"
    if BUILD:
        built = Path(BUILD) / path.strip("/")
        html = built / "index.html" if (built / "index.html").exists() or key == "/" else built.with_suffix(".html")
        assert html.exists(), f"{route} -> {url}: not in the docs build"
        if anchor:
            assert f'id="{anchor}"' in html.read_text(encoding="utf-8"), f"{route} -> {url}: anchor not rendered"


def test_anchor_check_rejects_a_missing_heading():
    md = _routes()["/features/orders"]
    assert "products--bill-of-materials" in _anchors(md)
    assert "no-such-heading" not in _anchors(md)
    assert "/admin/settings" not in _routes()
    # relevance check has teeth: the old Settings target page never mentions the Settings page's topics
    assert "audit log" not in _sections(_routes()["/features/scheduler"])[""]
