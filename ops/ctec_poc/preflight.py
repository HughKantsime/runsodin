"""Read-only, secret-free CTEC POC preflight report."""

from __future__ import annotations

import argparse
import html
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, text


@dataclass(frozen=True)
class Check:
    key: str
    label: str
    passed: bool
    required: bool
    detail: str


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def collect_checks(engine, env: dict[str, str], *, license_ready: bool, require_classroom: bool) -> list[Check]:
    checks: list[Check] = []
    trusted = [part.strip() for part in env.get("TRUSTED_HOSTS", "*").split(",") if part.strip()]
    checks.append(Check("trusted_hosts", "Exact trusted hostname", bool(trusted) and "*" not in trusted, True, "Configured" if trusted and "*" not in trusted else "TRUSTED_HOSTS must not contain *"))
    secure_cookie = _bool(env.get("COOKIE_SECURE"), True)
    checks.append(Check("secure_cookie", "Secure session cookie", secure_cookie, True, "Enabled" if secure_cookie else "COOKIE_SECURE must be true"))
    same_site = env.get("COOKIE_SAMESITE", "strict").lower()
    checks.append(Check("oidc_cookie", "OIDC-compatible SameSite policy", same_site in {"lax", "none"}, True, "Configured" if same_site in {"lax", "none"} else "Use lax for a normal HTTPS OIDC callback"))
    checks.append(Check("oidc_redirect", "External OIDC redirect URI", bool(env.get("OIDC_REDIRECT_URI")), True, "Configured" if env.get("OIDC_REDIRECT_URI") else "Set OIDC_REDIRECT_URI to the public HTTPS callback"))
    checks.append(Check("encryption", "At-rest secret encryption", bool(env.get("ENCRYPTION_KEY")), True, "Configured" if env.get("ENCRYPTION_KEY") else "ENCRYPTION_KEY is required"))
    checks.append(Check("education_license", "Education entitlement", license_ready, True, "Active" if license_ready else "Signed Education entitlement not observed"))

    with engine.connect() as connection:
        mode = connection.execute(text("SELECT value FROM system_config WHERE key='education_mode'")).fetchone()
        checks.append(Check("education_mode", "Education mode", bool(mode and mode.value == "true"), True, "Enabled" if mode and mode.value == "true" else "Disabled"))
        oidc = connection.execute(text("SELECT * FROM oidc_config WHERE id=1")).fetchone()
        oidc_data = oidc._mapping if oidc else {}
        provider = oidc_data.get("provider_type") or "microsoft"
        provider_ok = provider in {"microsoft", "google", "generic"}
        discovery_ok = bool(oidc_data.get("discovery_url")) or provider in {"microsoft", "google"}
        oidc_ready = bool(oidc_data.get("is_enabled") and oidc_data.get("client_id") and oidc_data.get("client_secret_encrypted") and oidc_data.get("default_group_id") and provider_ok and discovery_ok and (provider != "google" or oidc_data.get("allowed_domains")))
        checks.append(Check("oidc", "OIDC login", oidc_ready, True, f"{provider.title()} configured" if oidc_ready else "Provider configuration is incomplete"))
        pilot_org_id = int(oidc_data["default_group_id"]) if oidc_data.get("default_group_id") else None
        classroom = connection.execute(
            text(
                "SELECT state, refresh_token_encrypted FROM classroom_connections "
                "WHERE org_id=:org_id"
            ),
            {"org_id": pilot_org_id},
        ).fetchone() if pilot_org_id is not None else None
        classroom_ready = bool(classroom and classroom.state == "connected" and classroom.refresh_token_encrypted)
        checks.append(Check("classroom", "Google Classroom", classroom_ready, require_classroom, "Connected" if classroom_ready else "Not connected (optional unless selected for pilot)"))
        counts = connection.execute(text(
            "SELECT "
            "(SELECT COUNT(*) FROM education_cost_centers "
            " WHERE org_id=:org_id AND state='active') centers, "
            "(SELECT COUNT(*) FROM education_cost_center_grants g "
            " JOIN education_cost_centers c ON c.id=g.cost_center_id AND c.org_id=g.org_id "
            " WHERE g.org_id=:org_id AND g.state='active' AND g.role='student' "
            " AND c.state='active') students, "
            "(SELECT COUNT(*) FROM education_cost_center_grants g "
            " JOIN education_cost_centers c ON c.id=g.cost_center_id AND c.org_id=g.org_id "
            " WHERE g.org_id=:org_id AND g.state='active' AND g.role='manager' "
            " AND c.state='active') managers, "
            "(SELECT COUNT(*) FROM education_cost_center_printers cp "
            " JOIN education_cost_centers c ON c.id=cp.cost_center_id AND c.org_id=cp.org_id "
            " JOIN printers p ON p.id=cp.printer_id AND p.org_id=cp.org_id "
            " WHERE cp.org_id=:org_id AND cp.state='active' AND c.state='active' "
            " AND p.is_active IS TRUE) printers"
        ), {"org_id": pilot_org_id}).one()
        checks.extend([
            Check("centers", "Pilot class or club", counts.centers > 0, True, f"{counts.centers} active"),
            Check("students", "Pilot student roster", counts.students > 0, True, f"{counts.students} grants"),
            Check("managers", "Pilot teacher roster", counts.managers > 0, True, f"{counts.managers} grants"),
            Check("printers", "Authorized pilot printer", counts.printers > 0, True, f"{counts.printers} entitlements"),
        ])
    database_url = env.get("DATABASE_URL", "")
    backup_ready = database_url.startswith(("sqlite:///", "postgresql://", "postgresql+"))
    checks.append(Check("backup", "Backup workflow", backup_ready, True, "Database backend supported by ODIN backup verification" if backup_ready else "DATABASE_URL is not a supported backup backend"))
    return checks


def _render_html(payload: dict) -> str:
    rows = "".join(
        f"<tr><td>{html.escape(item['label'])}</td><td><span class='status {'pass' if item['passed'] else 'fail'}'>{'READY' if item['passed'] else ('OPTIONAL' if not item['required'] else 'ACTION')}</span></td><td>{html.escape(item['detail'])}</td></tr>"
        for item in payload["checks"]
    )
    border = "#3fb950" if payload["passed"] else "#f0a33a"
    summary = "All required configuration checks passed." if payload["passed"] else "One or more required checks need action."
    return f"""<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>ODIN CTEC POC Preflight</title><style>body{{margin:0;background:#0d1117;color:#e6edf3;font:15px system-ui,sans-serif}}main{{max-width:960px;margin:auto;padding:40px 20px}}h1{{margin-bottom:8px}}p{{color:#9da7b3}}table{{width:100%;border-collapse:collapse;margin-top:24px;background:#151b23;border:1px solid #30363d}}th,td{{padding:12px;text-align:left;border-bottom:1px solid #30363d}}th{{color:#9da7b3;font-size:12px;text-transform:uppercase}}.status{{font:700 11px ui-monospace,monospace}}.pass{{color:#3fb950}}.fail{{color:#f0a33a}}.summary{{padding:14px;border-left:3px solid {border};background:#151b23}}code{{color:#79c0ff}}</style></head><body><main><h1>ODIN CTEC POC Preflight</h1><p>Generated {html.escape(payload['generated_at'])}. Read-only and secret-free.</p><div class='summary'>{summary}</div><table><thead><tr><th>Check</th><th>State</th><th>Evidence</th></tr></thead><tbody>{rows}</tbody></table><p>This report does not certify untested physical printer models. The POC dispatch boundary remains sliced Bambu <code>.3mf</code>.</p></main></body></html>"""


def write_report(checks: list[Check], output_dir: Path, env: dict[str, str] | None = None) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(), "passed": all(item.passed for item in checks if item.required), "checks": [asdict(item) for item in checks]}
    serialized = json.dumps(payload, indent=2, sort_keys=True)
    source_env = env if env is not None else dict(os.environ)
    forbidden_values = [value for key, value in source_env.items() if any(word in key.upper() for word in ("SECRET", "TOKEN", "PASSWORD", "ENCRYPTION_KEY")) and len(value) >= 8]
    if any(value in serialized for value in forbidden_values):
        raise RuntimeError("Preflight output contains a secret value")
    (output_dir / "ctec-poc-preflight.json").write_text(serialized + "\n", encoding="utf-8")
    rendered = _render_html(payload)
    if any(value in rendered for value in forbidden_values):
        raise RuntimeError("Preflight HTML contains a secret value")
    (output_dir / "ctec-poc-preflight.html").write_text(rendered, encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="artifacts/ctec-poc-preflight")
    parser.add_argument("--require-classroom", action="store_true")
    args = parser.parse_args()
    database_url = os.environ.get("DATABASE_URL", "sqlite:///./odin.db")
    engine = create_engine(database_url)
    try:
        from license_manager import get_license
        license_info = get_license()
        license_ready = bool(license_info.valid and license_info.tier in {"education", "enterprise"} and license_info.has_feature("education_workflows"))
        checks = collect_checks(engine, dict(os.environ), license_ready=license_ready, require_classroom=args.require_classroom)
        payload = write_report(checks, Path(args.output_dir))
        print(Path(args.output_dir, "ctec-poc-preflight.html").resolve())
        return 0 if payload["passed"] else 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
