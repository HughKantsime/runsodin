"""Render compact, dependency-free release-control HTML evidence."""

from __future__ import annotations

import html
import json
from pathlib import Path


def render(result: dict[str, object], output: Path) -> None:
    status = str(result.get("status", "fail"))
    color = "#20b26b" if status == "pass" else "#dc3d4b"
    findings = result.get("findings", [])
    rows = "".join(f"<li>{html.escape(str(item))}</li>" for item in findings) or "<li>None</li>"
    counts = html.escape(json.dumps(result.get("counts", {}), sort_keys=True))
    gate = html.escape(str(result.get("gate_id", "unknown")))
    output.write_text(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ODIN release-control result</title><style>
body{{font:16px system-ui,sans-serif;max-width:900px;margin:48px auto;padding:0 20px;color:#16202a}}
.status{{color:{color};font-size:2rem;font-weight:800}}code{{background:#eef1f4;padding:.15rem .35rem;border-radius:4px}}
section{{border:1px solid #d9e0e6;border-radius:10px;padding:18px;margin:18px 0}}
</style></head><body><h1>ODIN Release Control</h1><p><code>{gate}</code></p>
<p class="status">{html.escape(status.upper())}</p><section><h2>Counts</h2><code>{counts}</code></section>
<section><h2>Findings</h2><ul>{rows}</ul></section></body></html>""", encoding="utf-8")
