#!/usr/bin/env python3
"""
O.D.I.N. Audit Report Generator

Reads artifacts from tests/artifacts/audit/ and produces a consolidated
findings report at tests/artifacts/audit/AUDIT_REPORT.md.

Run after `make test-audit` or standalone:
    python3 tests/audit_report.py
"""

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ARTIFACTS_DIR = Path(__file__).resolve().parent / "artifacts" / "audit"
E2E_DIR = ARTIFACTS_DIR / "e2e"
CONTRACTS_DIR = ARTIFACTS_DIR / "contracts"
REPORT_PATH = ARTIFACTS_DIR / "AUDIT_REPORT.md"


# ---------------------------------------------------------------------------
# Severity classification
# ---------------------------------------------------------------------------

SEVERITY_CRITICAL = "CRITICAL"
SEVERITY_HIGH = "HIGH"
SEVERITY_MEDIUM = "MEDIUM"
SEVERITY_LOW = "LOW"
SEVERITY_INFO = "INFO"

SEVERITY_ORDER = {SEVERITY_CRITICAL: 0, SEVERITY_HIGH: 1, SEVERITY_MEDIUM: 2, SEVERITY_LOW: 3, SEVERITY_INFO: 4}


def _severity_key(finding):
    return SEVERITY_ORDER.get(finding["severity"], 99)


# ---------------------------------------------------------------------------
# Artifact loaders
# ---------------------------------------------------------------------------

def _load_json(path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _load_route_artifacts():
    routes_dir = E2E_DIR / "routes"
    if not routes_dir.exists():
        return {}
    artifacts = {}
    for f in sorted(routes_dir.glob("*.json")):
        data = _load_json(f)
        if data:
            artifacts[f.stem] = data
    return artifacts


def _load_state_artifacts():
    states_dir = E2E_DIR / "printer-states"
    if not states_dir.exists():
        return {}
    artifacts = {}
    for f in sorted(states_dir.glob("*.json")):
        data = _load_json(f)
        if data:
            artifacts[f.stem] = data
    return artifacts


def _load_contract(name):
    path = CONTRACTS_DIR / name
    return _load_json(path) if path.exists() else None


# ---------------------------------------------------------------------------
# Analysis: Route health
# ---------------------------------------------------------------------------

def analyze_routes(routes):
    findings = []
    healthy = []
    errored = []

    for name, data in routes.items():
        console_errors = data.get("console_errors", [])
        page_errors = data.get("page_errors", [])
        server_failures = data.get("server_failures", [])
        auth_failures = data.get("auth_failures", [])
        api_count = data.get("api_success_count", 0)
        path = data.get("path", f"/{name}")

        issues = []
        if page_errors:
            issues.append(("page_error", page_errors))
        if server_failures:
            issues.append(("server_5xx", server_failures))
        if auth_failures:
            issues.append(("auth_failure", auth_failures))
        if console_errors:
            issues.append(("console_error", console_errors))

        if not issues:
            healthy.append(name)
            continue

        errored.append(name)
        for issue_type, errors in issues:
            if issue_type == "page_error":
                severity = SEVERITY_CRITICAL
                category = "Runtime Error"
            elif issue_type == "server_5xx":
                severity = SEVERITY_HIGH
                category = "Server Error (5xx)"
            elif issue_type == "auth_failure":
                severity = SEVERITY_MEDIUM
                category = "Auth Failure"
            else:
                severity = SEVERITY_MEDIUM
                category = "Console Error"

            # Deduplicate similar errors
            unique_errors = list(dict.fromkeys(errors))
            for err in unique_errors[:3]:
                findings.append({
                    "severity": severity,
                    "category": category,
                    "route": name,
                    "path": path,
                    "detail": _truncate(err, 200),
                    "source": "e2e-route",
                })

    return findings, healthy, errored


# ---------------------------------------------------------------------------
# Analysis: API contract — read endpoints
# ---------------------------------------------------------------------------

def analyze_read_contracts(data):
    if not data:
        return []
    findings = []
    for result in data.get("results", []):
        status = result.get("status_code")
        res = result.get("result", "")
        path = result.get("path", "")

        if status and status >= 500:
            findings.append({
                "severity": SEVERITY_HIGH,
                "category": "API Server Error",
                "route": None,
                "path": f"GET {path}",
                "detail": f"Returned {status}",
                "source": "contract-read",
            })
        elif res == "shape-mismatch":
            findings.append({
                "severity": SEVERITY_MEDIUM,
                "category": "Response Schema Drift",
                "route": None,
                "path": f"GET {path}",
                "detail": "Response JSON does not match documented OpenAPI schema",
                "source": "contract-read",
            })
        elif res == "request-failed":
            error = result.get("error", "unknown")
            if "timed out" in error.lower():
                continue  # SSE/streaming endpoints — expected
            findings.append({
                "severity": SEVERITY_MEDIUM,
                "category": "Endpoint Unreachable",
                "route": None,
                "path": f"GET {path}",
                "detail": _truncate(error, 200),
                "source": "contract-read",
            })
    return findings


# ---------------------------------------------------------------------------
# Analysis: API contract — invalid payload handling
# ---------------------------------------------------------------------------

def analyze_invalid_writes(data):
    if not data:
        return []
    findings = []

    # Group by endpoint to avoid duplicate findings
    endpoint_issues = defaultdict(list)
    for result in data.get("results", []):
        status = result.get("status_code")
        res = result.get("result", "")
        method = result.get("method", "")
        path = result.get("path", "")
        case = result.get("case", "")
        endpoint_key = f"{method} {path}"

        if status and status >= 500:
            endpoint_issues[endpoint_key].append({
                "type": "500_on_invalid",
                "case": case,
                "status": status,
            })
        elif res == "accepted-invalid":
            endpoint_issues[endpoint_key].append({
                "type": "accepted_invalid",
                "case": case,
                "status": status,
            })

    for endpoint, issues in sorted(endpoint_issues.items()):
        crashes = [i for i in issues if i["type"] == "500_on_invalid"]
        acceptances = [i for i in issues if i["type"] == "accepted_invalid"]

        if crashes:
            cases = ", ".join(i["case"] for i in crashes)
            findings.append({
                "severity": SEVERITY_HIGH,
                "category": "Crashes on Invalid Input",
                "route": None,
                "path": endpoint,
                "detail": f"Returns 500 instead of 4xx for: {cases}",
                "source": "contract-write",
            })
        if acceptances:
            cases = ", ".join(i["case"] for i in acceptances)
            findings.append({
                "severity": SEVERITY_MEDIUM,
                "category": "Missing Input Validation",
                "route": None,
                "path": endpoint,
                "detail": f"Accepts invalid payload without error for: {cases}",
                "source": "contract-write",
            })

    return findings


# ---------------------------------------------------------------------------
# Analysis: Safe creates
# ---------------------------------------------------------------------------

def analyze_safe_creates(data):
    if not data:
        return []
    findings = []
    for result in data.get("results", []):
        res = result.get("result", "")
        path = result.get("path", "")
        if res == "create-failed":
            excerpt = result.get("response_excerpt", "")
            findings.append({
                "severity": SEVERITY_HIGH,
                "category": "Create Endpoint Broken",
                "route": None,
                "path": f"POST {path}",
                "detail": _truncate(excerpt, 200),
                "source": "contract-create",
            })
        elif res == "cleanup-failed":
            findings.append({
                "severity": SEVERITY_MEDIUM,
                "category": "Delete Endpoint Broken",
                "route": None,
                "path": f"DELETE {path}/{result.get('created_id')}",
                "detail": f"Cleanup returned {result.get('cleanup_status')}",
                "source": "contract-create",
            })
    return findings


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

def _truncate(s, max_len):
    s = str(s).replace("\n", " ").strip()
    return s[:max_len] + "..." if len(s) > max_len else s


def _stats_section(routes, healthy, errored, read_data, write_data, create_data):
    total_routes = len(routes)
    total_read = len(read_data.get("results", [])) if read_data else 0
    read_ok = sum(1 for r in (read_data or {}).get("results", []) if r.get("result") in ("ok", "non-json-ok", "stream-or-download"))
    total_write = len(write_data.get("results", [])) if write_data else 0
    write_ok = sum(1 for r in (write_data or {}).get("results", []) if r.get("result") == "rejected-or-tolerated")
    total_create = len(create_data.get("results", [])) if create_data else 0
    create_ok = sum(1 for r in (create_data or {}).get("results", []) if r.get("result") == "ok")

    lines = [
        "## Coverage Summary\n",
        f"| Layer | Tested | Passed | Pass Rate |",
        f"|-------|--------|--------|-----------|",
        f"| UI Routes | {total_routes} | {len(healthy)} | {_pct(len(healthy), total_routes)} |",
        f"| API Read Endpoints | {total_read} | {read_ok} | {_pct(read_ok, total_read)} |",
        f"| Invalid Payload Rejection | {total_write} | {write_ok} | {_pct(write_ok, total_write)} |",
        f"| Create + Cleanup | {total_create} | {create_ok} | {_pct(create_ok, total_create)} |",
        "",
    ]
    return "\n".join(lines)


def _pct(n, total):
    if total == 0:
        return "N/A"
    return f"{n/total*100:.0f}%"


def _findings_section(findings):
    if not findings:
        return "## Findings\n\nNo issues found.\n"

    lines = ["## Findings\n"]

    by_severity = defaultdict(list)
    for f in findings:
        by_severity[f["severity"]].append(f)

    for severity in [SEVERITY_CRITICAL, SEVERITY_HIGH, SEVERITY_MEDIUM, SEVERITY_LOW, SEVERITY_INFO]:
        group = by_severity.get(severity, [])
        if not group:
            continue

        lines.append(f"### {severity} ({len(group)})\n")
        lines.append(f"| # | Category | Endpoint | Detail |")
        lines.append(f"|---|----------|----------|--------|")

        for idx, f in enumerate(group, 1):
            path = f.get("path", "")
            route = f.get("route")
            endpoint = f"`{path}`" if path else ""
            if route:
                endpoint = f"`/{route}` ({path})" if path != f"/{route}" else f"`/{route}`"
            detail = f["detail"].replace("|", "\\|")
            lines.append(f"| {idx} | {f['category']} | {endpoint} | {detail} |")

        lines.append("")

    return "\n".join(lines)


def _healthy_routes_section(healthy):
    if not healthy:
        return ""
    return (
        "## Healthy Routes\n\n"
        + ", ".join(f"`/{r}`" for r in healthy)
        + "\n\n"
        + f"These {len(healthy)} routes rendered without errors, handled safe interactions correctly, "
        + "and made successful API calls.\n"
    )


def _recommendations_section(findings):
    if not findings:
        return ""

    lines = ["## Recommended Actions\n"]
    priorities = []

    critical = [f for f in findings if f["severity"] == SEVERITY_CRITICAL]
    high = [f for f in findings if f["severity"] == SEVERITY_HIGH]
    medium = [f for f in findings if f["severity"] == SEVERITY_MEDIUM]

    crashes_500 = [f for f in high if f["category"] == "Crashes on Invalid Input"]
    server_errors = [f for f in findings if f["category"] == "API Server Error"]
    validation_gaps = [f for f in medium if f["category"] == "Missing Input Validation"]
    runtime_errors = [f for f in critical if f["category"] == "Runtime Error"]

    if runtime_errors:
        routes = list(set(f["route"] for f in runtime_errors if f.get("route")))
        priorities.append(
            f"1. **Fix runtime crashes** on {', '.join(f'`/{r}`' for r in routes)}. "
            f"These pages are broken for all users."
        )
    if server_errors:
        endpoints = list(set(f["path"] for f in server_errors))
        priorities.append(
            f"{'2' if priorities else '1'}. **Fix server 500 errors** on {len(endpoints)} endpoint(s): "
            + ", ".join(f"`{e}`" for e in endpoints[:5])
            + (f" and {len(endpoints)-5} more" if len(endpoints) > 5 else "")
            + ". These return unhandled exceptions to clients."
        )
    if crashes_500:
        endpoints = [f["path"] for f in crashes_500]
        priorities.append(
            f"{'3' if len(priorities) >= 2 else len(priorities)+1}. "
            f"**Add input validation** to {len(endpoints)} endpoint(s) that crash (500) on invalid input: "
            + ", ".join(f"`{e}`" for e in endpoints[:5])
            + (f" and {len(endpoints)-5} more" if len(endpoints) > 5 else "")
            + ". Malformed requests should return 400/422, not crash."
        )
    if validation_gaps:
        endpoints = [f["path"] for f in validation_gaps]
        n = len(endpoints)
        priorities.append(
            f"{len(priorities)+1}. **Tighten input validation** on {n} endpoint(s) that silently accept "
            f"wrong-typed payloads. Low blast radius (most are admin-only) but creates "
            f"data integrity risk."
        )

    if not priorities:
        return ""

    for p in priorities:
        lines.append(p)
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def generate_report():
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    routes = _load_route_artifacts()
    read_data = _load_contract("read_endpoints.json")
    write_data = _load_contract("invalid_writes.json")
    create_data = _load_contract("safe_creates.json")

    # Collect findings
    all_findings = []

    route_findings, healthy, errored = analyze_routes(routes)
    all_findings.extend(route_findings)
    all_findings.extend(analyze_read_contracts(read_data))
    all_findings.extend(analyze_invalid_writes(write_data))
    all_findings.extend(analyze_safe_creates(create_data))

    # Sort by severity
    all_findings.sort(key=_severity_key)

    # Deduplicate (same path + category)
    seen = set()
    deduped = []
    for f in all_findings:
        key = (f["path"], f["category"], f["detail"][:80])
        if key not in seen:
            seen.add(key)
            deduped.append(f)
    all_findings = deduped

    # Render
    report = []
    report.append(f"# O.D.I.N. Audit Report")
    report.append(f"")
    report.append(f"**Generated:** {timestamp}")
    report.append(f"**Findings:** {len(all_findings)} "
                  f"({sum(1 for f in all_findings if f['severity'] == SEVERITY_CRITICAL)} critical, "
                  f"{sum(1 for f in all_findings if f['severity'] == SEVERITY_HIGH)} high, "
                  f"{sum(1 for f in all_findings if f['severity'] == SEVERITY_MEDIUM)} medium)")
    report.append("")
    report.append(_stats_section(routes, healthy, errored, read_data, write_data, create_data))
    report.append(_findings_section(all_findings))
    report.append(_recommendations_section(all_findings))
    report.append(_healthy_routes_section(healthy))

    text = "\n".join(report)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(text)
    print(f"Audit report written to {REPORT_PATH}")
    print(f"  {len(all_findings)} findings across {len(routes)} routes + {len((read_data or {}).get('results', []))} API endpoints")

    return len(all_findings)


if __name__ == "__main__":
    count = generate_report()
    sys.exit(1 if count > 0 else 0)
