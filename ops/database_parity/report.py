"""Dependency-free HTML report for the database parity gate."""

from __future__ import annotations

from html import escape
from pathlib import Path


def _text(value: object) -> str:
    return escape(str(value), quote=True)


def render_report(manifest: dict[str, object], output: Path) -> None:
    status = str(manifest.get("status", "FAIL")).upper()
    if status not in {"PASS", "FAIL"}:
        status = "FAIL"
    color = "#2ea043" if status == "PASS" else "#f85149"
    phases = manifest.get("phases", [])
    rows = "".join(
        "<tr>"
        f"<td>{_text(phase.get('name', 'unknown'))}</td>"
        f"<td>{_text(phase.get('status', 'FAIL'))}</td>"
        f"<td>{_text(phase.get('duration_seconds', ''))}</td>"
        f"<td>{_text(phase.get('detail', ''))}</td>"
        "</tr>"
        for phase in phases
        if isinstance(phase, dict)
    ) or '<tr><td colspan="4">No phases recorded</td></tr>'
    evidence = "".join(
        f'<li><a href="{_text(path.relative_to(output.parent).as_posix())}">'
        f"{_text(path.relative_to(output.parent).as_posix())}</a></li>"
        for path in sorted(output.parent.rglob("*"))
        if path.is_file() and path != output
    ) or "<li>No retained evidence</li>"
    measurements = manifest.get("database_evidence", [])
    measurement_rows = "".join(
        "<tr>"
        f"<td>{_text(item.get('dialect', 'unknown'))}</td>"
        f"<td>{_text(item.get('attempt', ''))}</td>"
        f"<td>{_text(item.get('backup_size_bytes', ''))}</td>"
        f"<td>{_text(item.get('table_count', ''))}</td>"
        f"<td>{_text(item.get('toc_entries', ''))}</td>"
        f"<td>{_text(item.get('backup_duration_seconds', ''))}</td>"
        f"<td>{_text(item.get('validation_duration_seconds', ''))}</td>"
        f"<td>{_text(item.get('restore_duration_seconds', ''))}</td>"
        f"<td><code>{_text(str(item.get('toc_fingerprint') or 'n/a')[:12])}</code></td>"
        f"<td><code>{_text(str(item.get('schema_fingerprint', ''))[:12])}</code></td>"
        f"<td><code>{_text(str(item.get('relationship_graph_fingerprint', ''))[:12])}</code></td>"
        "</tr>"
        for item in measurements
        if isinstance(item, dict)
    ) or '<tr><td colspan="11">No database measurements recorded</td></tr>'
    posture = manifest.get("runtime_posture", {})
    warning_items = "".join(
        f"<li>{_text(item)}</li>"
        for item in posture.get("classified_warnings", [])
    ) if isinstance(posture, dict) else ""
    topology = manifest.get("topology_evidence", [])
    topology_rows = "".join(
        "<tr>"
        f"<td>{_text(item.get('attempt', ''))}</td>"
        f"<td><code>{_text(str(item.get('api', ''))[:20])}</code></td>"
        f"<td><code>{_text(str(item.get('worker', ''))[:20])}</code></td>"
        f"<td><code>{_text(str(item.get('postgres_image_id', ''))[:20])}</code></td>"
        f"<td><code>{_text(item.get('postgres_digest', ''))}</code></td>"
        "</tr>"
        for item in topology
        if isinstance(item, dict)
    ) or '<tr><td colspan="5">No topology evidence recorded</td></tr>'
    output.write_text(
        f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ODIN Database Parity — {_text(status)}</title>
<style>
:root{{color-scheme:dark;font-family:Inter,ui-sans-serif,system-ui,sans-serif}}
body{{margin:0;background:#0d1117;color:#e6edf3}}main{{width:min(1000px,calc(100% - 32px));margin:36px auto}}
header,section{{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:20px;margin-bottom:16px}}
.status{{color:{color};font:700 2rem ui-monospace,monospace}}.meta{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}}
.meta div{{background:#0d1117;border-radius:8px;padding:12px;overflow-wrap:anywhere}}table{{width:100%;border-collapse:collapse}}
th,td{{padding:10px;border-bottom:1px solid #30363d;text-align:left;vertical-align:top}}a{{color:#58a6ff}}
</style></head><body><main>
<header><div class="status">{_text(status)}</div><h1>ODIN SQLite/PostgreSQL Parity Gate</h1>
<div class="meta"><div><strong>Run</strong><br>{_text(manifest.get('run_id', 'unknown'))}</div>
<div><strong>Commit</strong><br>{_text(manifest.get('commit', 'unknown'))}</div>
<div><strong>Dirty tree</strong><br>{_text(manifest.get('dirty', 'unknown'))}</div>
<div><strong>Candidate image</strong><br>{_text(manifest.get('candidate_image_id', 'unknown'))}</div></div></header>
<section><h2>Phases</h2><table><thead><tr><th>Phase</th><th>Status</th><th>Seconds</th><th>Evidence</th></tr></thead><tbody>{rows}</tbody></table></section>
<section><h2>Backup and Restore Measurements</h2><table><thead><tr><th>Database</th><th>Attempt</th><th>Bytes</th><th>Tables</th><th>TOC</th><th>Backup s</th><th>Validation s</th><th>Restore s</th><th>TOC SHA</th><th>Schema SHA</th><th>Graph SHA</th></tr></thead><tbody>{measurement_rows}</tbody></table></section>
<section><h2>Exact-image PostgreSQL Topology</h2><table><thead><tr><th>Attempt</th><th>API/bootstrap image</th><th>Non-owner worker image</th><th>PostgreSQL image ID</th><th>PostgreSQL digest</th></tr></thead><tbody>{topology_rows}</tbody></table></section>
<section><h2>Test Runtime Posture</h2><p><strong>Network:</strong> {_text(posture.get('network', 'unknown') if isinstance(posture, dict) else 'unknown')}</p><p><strong>Authentication:</strong> {_text(posture.get('authentication', 'unknown') if isinstance(posture, dict) else 'unknown')}</p><h3>Classified warnings</h3><ul>{warning_items or '<li>None</li>'}</ul></section>
<section><h2>Retained Evidence</h2><ul>{evidence}</ul></section>
</main></body></html>""",
        encoding="utf-8",
    )
