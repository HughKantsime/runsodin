"""Standalone HTML evidence report for a candidate-gate run."""

from __future__ import annotations

from html import escape
from pathlib import Path


def _text(value: object) -> str:
    return escape(str(value), quote=True)


def render_report(manifest: dict, output: Path) -> None:
    """Render the redacted manifest as a truthful, dependency-free HTML report."""
    status = str(manifest.get("status", "FAIL")).upper()
    if status not in {"PASS", "FAIL"}:
        status = "FAIL"
    color = "#2ea043" if status == "PASS" else "#f85149"
    phases = manifest.get("phases", [])
    phase_rows = "".join(
        "<tr>"
        f"<td>{_text(phase.get('name', 'unknown'))}</td>"
        f"<td>{_text(phase.get('status', 'FAIL'))}</td>"
        f"<td>{_text(phase.get('detail', ''))}</td>"
        "</tr>"
        for phase in phases
    ) or '<tr><td colspan="3">No phases recorded</td></tr>'
    fixtures = manifest.get("fixtures", {})
    fixture_rows = "".join(
        f"<tr><td>{_text(key)}</td><td>{_text(value)}</td></tr>"
        for key, value in sorted(fixtures.items())
    ) or '<tr><td colspan="2">No fixture manifest recorded</td></tr>'
    evidence_files = sorted(
        path.relative_to(output.parent).as_posix()
        for path in output.parent.rglob("*")
        if path.is_file() and path != output
    )
    evidence_links = "".join(
        f'<li><a href="{_text(path)}">{_text(path)}</a></li>' for path in evidence_files
    ) or "<li>No retained evidence files</li>"
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ODIN Candidate Gate — {_text(status)}</title>
  <style>
    :root {{ color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }}
    body {{ margin: 0; background: #0d1117; color: #e6edf3; }}
    main {{ width: min(960px, calc(100% - 32px)); margin: 36px auto; }}
    header, section {{ background: #161b22; border: 1px solid #30363d; border-radius: 12px; padding: 20px; margin-bottom: 16px; }}
    h1, h2 {{ margin-top: 0; }}
    .status {{ color: {color}; font: 700 2rem ui-monospace, monospace; }}
    .meta {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; }}
    .meta div {{ background: #0d1117; border-radius: 8px; padding: 12px; overflow-wrap: anywhere; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ padding: 10px; border-bottom: 1px solid #30363d; text-align: left; vertical-align: top; }}
    a {{ color: #58a6ff; }}
  </style>
</head>
<body><main>
  <header>
    <div class="status">{_text(status)}</div>
    <h1>ODIN Full-Stack Candidate Gate</h1>
    <div class="meta">
      <div><strong>Run</strong><br>{_text(manifest.get('run_id', 'unknown'))}</div>
      <div><strong>Commit</strong><br>{_text(manifest.get('commit', 'unknown'))}</div>
      <div><strong>Dirty tree</strong><br>{_text(manifest.get('dirty', 'unknown'))}</div>
      <div><strong>Candidate image</strong><br>{_text(manifest.get('candidate_image_id', 'unknown'))}</div>
      <div><strong>Running image</strong><br>{_text(manifest.get('running_image_id', 'unknown'))}</div>
    </div>
  </header>
  <section><h2>Phases</h2><table><thead><tr><th>Phase</th><th>Status</th><th>Evidence</th></tr></thead><tbody>{phase_rows}</tbody></table></section>
  <section><h2>Fixtures</h2><table><thead><tr><th>Entity</th><th>Count</th></tr></thead><tbody>{fixture_rows}</tbody></table></section>
  <section><h2>Retained Evidence</h2><ul>{evidence_links}</ul></section>
</main></body></html>
"""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html, encoding="utf-8")
