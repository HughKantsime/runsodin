"""Static, dependency-free HTML evidence for the Education sandbox lifecycle."""

from __future__ import annotations

import json
from html import escape
from pathlib import Path


def _text(value: object) -> str:
    return escape(str(value), quote=True)


def render_report(manifest: dict[str, object], output: Path) -> None:
    status = str(manifest.get("status", "FAIL")).upper()
    colors = {"PASS": "#3fb950", "FAIL": "#f85149", "BLOCKED_EXTERNAL": "#d29922"}
    if status not in colors:
        status = "FAIL"
    phases = manifest.get("phases", [])
    rows = "".join(
        "<tr>"
        f"<td>{_text(item.get('name', 'unknown'))}</td>"
        f"<td>{_text(item.get('status', 'FAIL'))}</td>"
        f"<td>{_text(item.get('detail', ''))}</td>"
        "</tr>"
        for item in phases
        if isinstance(item, dict)
    ) or '<tr><td colspan="3">No phases recorded</td></tr>'
    evidence = _text(json.dumps(manifest.get("evidence", {}), indent=2, sort_keys=True))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ODIN EDU Sandbox — {_text(status)}</title><style>
:root{{color-scheme:dark;font-family:Inter,ui-sans-serif,system-ui,sans-serif}}
body{{margin:0;background:#0d1117;color:#e6edf3}}main{{width:min(980px,calc(100% - 32px));margin:36px auto}}
header,section{{background:#161b22;border:1px solid #30363d;border-radius:14px;padding:22px;margin-bottom:16px}}
.status{{font:800 2rem ui-monospace,monospace;color:{colors[status]}}}.meta{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}}
.meta div{{background:#0d1117;border-radius:8px;padding:12px;overflow-wrap:anywhere}}table{{width:100%;border-collapse:collapse}}
th,td{{padding:10px;border-bottom:1px solid #30363d;text-align:left;vertical-align:top}}code{{overflow-wrap:anywhere}}a{{color:#58a6ff}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#0d1117;border-radius:8px;padding:14px}}
</style></head><body><main><header><div class="status">{_text(status)}</div><h1>ODIN Education Sandbox Evidence</h1>
<div class="meta"><div><strong>Sandbox</strong><br>{_text(manifest.get('sandbox_id', 'unknown'))}</div>
<div><strong>Run</strong><br>{_text(manifest.get('run_id', 'unknown'))}</div>
<div><strong>Commit</strong><br><code>{_text(manifest.get('source_commit', 'unknown'))}</code></div>
<div><strong>Candidate image</strong><br><code>{_text(manifest.get('candidate_image_id', 'unknown'))}</code></div></div></header>
<section><h2>Lifecycle phases</h2><table><thead><tr><th>Check</th><th>Status</th><th>Evidence</th></tr></thead><tbody>{rows}</tbody></table></section>
<section><h2>Retained proof</h2><pre>{evidence}</pre></section>
<section><h2>Interpretation</h2><p>{_text(manifest.get('summary', 'No summary recorded.'))}</p>
<p>This report contains hashes and non-secret facts only. A production-verifiable, installation-bound Education license is required before activation can pass.</p></section>
</main></body></html>""",
        encoding="utf-8",
    )
