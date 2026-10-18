"""Every FEATURES.md feature section keeps its bullets under a ### subsection.

The site sync (odin-site scripts/sync-from-odin.mjs) reads each '## N.' section's
'### N.M' subsections; bullets directly under the '##' heading are dropped, which
left the site's section 7 (Consumables) empty."""
import re
from pathlib import Path

FEATURES = (Path(__file__).resolve().parents[2] / "FEATURES.md").read_text(encoding="utf-8")


def test_numbered_sections_put_bullets_under_a_subsection():
    orphans = []
    section = None
    in_subsection = False
    for line in FEATURES.splitlines():
        if re.match(r"^## \d+\. ", line):
            section, in_subsection = line, False
        elif line.startswith("## "):
            section, in_subsection = None, False
        elif line.startswith("### "):
            in_subsection = True
        elif section and not in_subsection and line.startswith("- "):
            orphans.append((section, line))
    assert orphans == []
