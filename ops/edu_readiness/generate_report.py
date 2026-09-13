"""Generate a self-contained, escaped HTML report from EDU readiness evidence."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path


LEGAL_SOURCES = Path(__file__).with_name("legal_sources.json")
HARDWARE_COMPATIBILITY = Path(__file__).with_name("hardware-compatibility.json")


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _load_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _result_map(run_dir: Path) -> dict[str, dict]:
    results: dict[str, dict] = {}
    for path in sorted(run_dir.glob("*.json")):
        if path.name in {"summary.json", "acceptances.json"}:
            continue
        loaded = _load_json(path, {})
        if isinstance(loaded, dict):
            results[str(loaded.get("gate_id", path.stem))] = loaded
    return results


def _status_badge(status: object) -> str:
    normalized = str(status).lower()
    css_class = normalized if normalized in {"pass", "fail", "blocked"} else "fail"
    return f'<span class="status {css_class}">{_escape(normalized.upper())}</span>'


def _gate_rows(results: dict[str, dict]) -> str:
    rows = []
    for gate_id, result in sorted(results.items()):
        findings = "; ".join(str(item) for item in result.get("findings", [])) or "None"
        rows.append(
            "<tr>"
            f"<td><code>{_escape(gate_id)}</code></td>"
            f"<td>{_status_badge(result.get('status', 'fail'))}</td>"
            f"<td>{int(result.get('executed_count', 0))}</td>"
            f"<td>{_escape(findings)}</td>"
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="4">No gate results were recorded.</td></tr>'


def _blocker_items(summary: dict, results: dict[str, dict]) -> str:
    gate_ids = sorted(set(
        summary.get("missing_gates", [])
        + summary.get("failed_gates", [])
        + summary.get("blocked_gates", [])
        + summary.get("invalid_pass_gates", [])
    ))
    items = []
    for gate_id in gate_ids:
        result = results.get(gate_id, {})
        findings = "; ".join(str(item) for item in result.get("findings", []))
        detail = findings or ("No result artifact exists." if not result else "No detailed finding recorded.")
        items.append(f"<li><code>{_escape(gate_id)}</code> — {_escape(detail)}</li>")
    if items:
        return "".join(items)
    if summary.get("scope") == "code-controlled":
        return (
            "<li>None in the deterministic code-controlled scope. Live TLS, "
            "legal-source, physical-device, and manual contract rows were not evaluated.</li>"
        )
    return "<li>None. All mandatory rows passed.</li>"


def _load_table(result: dict) -> str:
    repetitions = result.get("metrics", {}).get("repetitions", [])
    rows = []
    for repetition in repetitions:
        rows.append(
            "<tr>"
            f"<td>{int(repetition.get('repetition', 0))}</td>"
            f"<td>{int(repetition.get('operations', 0))}</td>"
            f"<td>{_escape(repetition.get('read_p95_ms', '—'))}</td>"
            f"<td>{_escape(repetition.get('write_p95_ms', '—'))}</td>"
            f"<td>{_escape(repetition.get('global_p99_ms', '—'))}</td>"
            f"<td>{int(repetition.get('errors', 0))}</td>"
            f"<td>{int(repetition.get('tenant_leaks', 0))}</td>"
            f"<td>{int(repetition.get('websocket_failures', 0))}</td>"
            f"<td>{int(repetition.get('websocket_messages_exchanged', 0))}</td>"
            f"<td>{int(repetition.get('websocket_isolation_checks', 0))}</td>"
            f"<td>{int(repetition.get('websocket_cross_user_events', 0))}</td>"
            "</tr>"
        )
    if not rows:
        return "<p>No API load repetition evidence was recorded.</p>"
    return (
        '<div class="table-wrap"><table><thead><tr><th>Run</th><th>Operations</th>'
        '<th>Read p95 ms</th><th>Write p95 ms</th><th>Global p99 ms</th>'
        '<th>Errors</th><th>Tenant leaks</th><th>WS failures</th><th>WS messages</th>'
        '<th>WS isolation checks</th><th>Cross-user WS events</th></tr></thead><tbody>'
        + "".join(rows) + "</tbody></table></div>"
    )


def _evidence_cards(results: dict[str, dict]) -> str:
    labels = {
        "foundation": "Foundation", "privacy": "Privacy",
        "backup_restore": "Backup / restore", "hardware_contracts": "Hardware contracts",
        "api_load": "API / WebSocket load", "accessibility": "Accessibility", "security": "Security + operations",
        "artifact_scan": "Artifact safety",
    }
    cards = []
    for gate_id, label in labels.items():
        result = results.get(gate_id, {})
        cards.append(
            '<div class="card">'
            f"<span>{_escape(label)}</span><strong>{int(result.get('executed_count', 0))}</strong>"
            f"<small>{_escape(str(result.get('status', 'missing')).upper())}</small></div>"
        )
    return "".join(cards)


def _hardware_table(results: dict[str, dict]) -> str:
    manifest = _load_json(HARDWARE_COMPATIBILITY, {"protocols": []})
    protocols = manifest.get("protocols", []) if isinstance(manifest, dict) else []
    rows = []
    for protocol in protocols:
        protocol_id = str(protocol.get("id", "unknown"))
        live = results.get(f"hardware_{protocol_id}_live", {})
        rows.append(
            "<tr>"
            f"<td>{_escape(protocol_id)}</td><td>{_escape(protocol.get('passive_transport', 'unknown'))}</td>"
            f"<td>{_escape(protocol.get('replay', 'unknown'))}</td>"
            f"<td>{_escape(protocol.get('parser', 'unknown'))}</td>"
            f"<td>{_escape(', '.join(protocol.get('exercise', [])))}</td>"
            f"<td>{_escape(protocol.get('observe', 'unknown'))}</td>"
            f"<td>{_escape(protocol.get('physical_evidence', 'unknown'))}</td>"
            f"<td>{_status_badge(live.get('status', 'blocked'))}</td></tr>"
        )
    return "".join(rows) or '<tr><td colspan="8">No compatibility manifest was found.</td></tr>'


def _source_items(results: dict[str, dict]) -> str:
    manifest = _load_json(LEGAL_SOURCES, {"sources": []})
    sources = manifest.get("sources", []) if isinstance(manifest, dict) else []
    observations = {
        item.get("id"): item
        for item in results.get("legal_sources_live", {}).get("metrics", {}).get("observations", [])
        if isinstance(item, dict)
    }
    items = []
    for source in sources:
        source_id = source.get("id", "unknown")
        observation = observations.get(source_id, {})
        access_text = f"manifest checked {source.get('accessed_at', 'not recorded')}"
        if observation.get("accessed_at"):
            access_text += f"; live fetch {observation['accessed_at']}"
        markers = "; ".join(str(item) for item in source.get("required_markers", []))
        items.append(
            "<li>"
            f'<a href="{_escape(source.get("url", "#"))}">{_escape(source_id)}</a>'
            f" — {_escape(access_text)}. Recorded facts/markers: {_escape(markers)}.</li>"
        )
    return "".join(items) or "<li>No primary-source manifest was found.</li>"


def render_report(run_dir: Path, title: str = "ODIN EDU Readiness") -> str:
    summary = _load_json(run_dir / "summary.json", {})
    if not isinstance(summary, dict):
        summary = {}
    results = _result_map(run_dir)
    run_id = next((result.get("run_id") for result in results.values() if result.get("run_id")), "unknown")
    accessibility = results.get("accessibility", {}).get("metrics", {})
    accessibility_note = (
        f"{accessibility.get('matrix_cases', 0)} automated axe matrix cases and "
        f"{accessibility.get('keyboard_cases', 0)} keyboard cases were recorded."
    )
    axe_impacts = accessibility.get("axe_findings_by_impact", {})
    axe_rules = accessibility.get("nonblocking_axe_rule_ids", {})
    accessibility_findings = (
        f"Critical: {int(axe_impacts.get('critical', 0))}; "
        f"serious: {int(axe_impacts.get('serious', 0))}; "
        f"moderate: {int(axe_impacts.get('moderate', 0))} "
        f"({', '.join(axe_rules.get('moderate', [])) or 'none'}); "
        f"minor: {int(axe_impacts.get('minor', 0))} "
        f"({', '.join(axe_rules.get('minor', [])) or 'none'})."
    )
    privacy_browser = _load_json(run_dir / "raw" / "privacy-browser.json", {"results": []})
    privacy_browser_results = privacy_browser.get("results", []) if isinstance(privacy_browser, dict) else []
    privacy_browser_passed = sum(1 for item in privacy_browser_results if item.get("pass"))
    scope = str(summary.get("scope", "full"))
    status_label = (
        f"Code-controlled gates: {summary.get('status', 'NOT_READY')}"
        if scope == "code-controlled"
        else f"Overall readiness: {summary.get('status', 'NOT_READY')}"
    )
    gate_scope_label = "code-controlled" if scope == "code-controlled" else "mandatory"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_escape(title)}</title><style>
:root{{--bg:#101318;--card:#1a2029;--card2:#222a35;--text:#f5f7fa;--muted:#bdc7d5;--line:#3b4655;--green:#67d58a;--red:#ff7b7b;--yellow:#ffd166;--link:#8fcdff}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:16px/1.55 system-ui,sans-serif}}main{{max-width:1180px;margin:auto;padding:32px 20px}}
h1{{margin:0 0 4px}}h2{{margin-top:0}}.lede{{color:var(--muted)}}.overall{{font-size:1.3rem;font-weight:800}}section{{background:var(--card);padding:20px;margin:20px 0;border-radius:12px;border:1px solid var(--line)}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:12px}}.card{{background:var(--card2);padding:14px;border-radius:9px;display:grid}}.card span,.card small{{color:var(--muted)}}.card strong{{font-size:1.8rem}}
.table-wrap{{overflow-x:auto}}table{{width:100%;border-collapse:collapse;min-width:720px}}th,td{{padding:10px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}}th{{color:var(--muted)}}
.status{{font-weight:800}}.pass{{color:var(--green)}}.fail{{color:var(--red)}}.blocked{{color:var(--yellow)}}a{{color:var(--link)}}code{{font-family:ui-monospace,monospace}}li{{margin:.45rem 0}}
@media print{{:root{{--bg:#fff;--card:#fff;--card2:#f2f4f7;--text:#111;--muted:#394150;--line:#bbc2cc;--link:#0645ad}}section{{break-inside:avoid}}}}
</style></head><body><main>
<h1>{_escape(title)}</h1><p class="overall">{_escape(status_label)}</p>
<p class="lede">Run <code>{_escape(run_id)}</code> · {int(summary.get('passed_gate_count', 0))} of {int(summary.get('required_gate_count', 0))} {_escape(gate_scope_label)} gate rows passed. This report distinguishes automated checks, live observations, and evidence that still requires a person or physical device.</p>
<section><h2>Evidence summary</h2><div class="cards">{_evidence_cards(results)}</div></section>
<section><h2>Current blockers and failures</h2><ul>{_blocker_items(summary, results)}</ul></section>
<section><h2>API and WebSocket load</h2>{_load_table(results.get('api_load', {}))}</section>
<section><h2>Compiled-browser privacy lifecycle</h2><p>{privacy_browser_passed} of {len(privacy_browser_results)} browser lifecycle cases passed. Covered flows are fresh login, reload, legacy-storage bootstrap, logout, session expiry, erasure, and post-erasure protected navigation; checks inspect local/session storage, Cache Storage, and IndexedDB.</p></section>
<section><h2>Accessibility evidence</h2><p>{_escape(accessibility_note)}</p><p>{_escape(accessibility_findings)}</p><p>Automated accessibility checks are a release baseline, not a substitute for manual keyboard, screen-reader, zoom/reflow, cognitive, or assistive-technology conformance testing.</p></section>
<section><h2>Hardware protocol compatibility</h2><div class="table-wrap"><table><thead><tr><th>Protocol</th><th>Passive transport</th><th>Replay</th><th>Parser</th><th>Authorized exercise</th><th>Observe</th><th>Physical evidence</th><th>Live row</th></tr></thead><tbody>{_hardware_table(results)}</tbody></table></div></section>
<section><h2>All gate evidence</h2><div class="table-wrap"><table><thead><tr><th>Gate</th><th>Status</th><th>Executed</th><th>Findings</th></tr></thead><tbody>{_gate_rows(results)}</tbody></table></div></section>
<section><h2>Primary legal and accessibility sources</h2><p>Dates below are repository attestation dates; live fetch dates appear only when the live source gate successfully records them. The DOJ Title II source records the applicable April 26, 2027 and April 26, 2028 compliance dates.</p><ul>{_source_items(results)}</ul></section>
<section><h2>Scope and limitations</h2><ul>
<li>Automated results are not legal advice, a legal certification, a VPAT, or an Accessibility Conformance Report.</li>
<li>Fixture/parser coverage does not prove compatibility with a physical printer or a specific firmware version; each live hardware row remains separate.</li>
<li>The load gate uses an in-process ASGI application, synthetic identities, pre-minted access/WebSocket tokens, an ephemeral signed Education license, and disposable SQLite. It does not measure login or WebSocket-token issuance latency; it detects regressions but is not production capacity evidence.</li>
<li>Backup automation currently verifies SQLite. PostgreSQL procedures remain operational runbook work and require a representative restore drill.</li>
<li>TLS and authoritative-source results are observations at the recorded run time, not continuing guarantees.</li>
<li>No student or customer data is used in this evidence bundle. School DPA/terms, breach notice, deletion certification, procurement acceptance, and W-9 handling require human approval.</li>
</ul></section>
</main></body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or args.run_dir / "index.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_report(args.run_dir), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
