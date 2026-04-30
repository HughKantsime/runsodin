"""Identity-scrub the AMS-swap fixture for the demo.subsystem.app reviewer env.

Replaces real-customer-identifiable values found in
`tests/fixtures/telemetry/bambu-x1c-ams-swap.jsonl` with synthetic ones,
emitting `.demo.jsonl` next to the source.

Audit (run --audit-only) reports:
  - LAN IPs (RFC1918, both string and Bambu little-endian uint32 form)
  - rtsp_url host
  - print job filenames in subtask_name
  - high-risk payload keys
  - (negative) emails, JWTs, MACs

Scrub rewrites only the fields documented below. Idempotent: re-running on
an already-scrubbed file is a no-op (the synthetic values are not on the
scrub list themselves).

Run from repo root:
    python ops/demo/scrub_fixture.py --audit-only
    python ops/demo/scrub_fixture.py --write
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SRC = REPO_ROOT / "tests" / "fixtures" / "telemetry" / "bambu-x1c-ams-swap.jsonl"
DST = REPO_ROOT / "tests" / "fixtures" / "telemetry" / "bambu-x1c-ams-swap.demo.jsonl"


REAL_LAN_IP = "192.168.72.210"
SCRUB_LAN_IP = "192.168.1.42"

REAL_LAN_IP_LE_UINT32 = 3527977152  # = ip_to_le_uint32("192.168.72.210")
SCRUB_LAN_IP_LE_UINT32 = 704751808  # = ip_to_le_uint32("192.168.1.42")

REAL_SUBTASK = "cabinet_lock_v3.1"
SCRUB_SUBTASK = "demo_widget_v1"

HIGH_RISK_KEYS = {
    "wifi_signal", "ssid", "lan_ip", "ipaddress", "ip_address",
    "user", "username", "user_id", "owner", "owner_id", "operator", "nickname",
    "token", "access_token", "auth_token", "bearer", "session_id",
    "email", "phone", "mac_address", "mac",
    "cloud_token", "secret", "password", "passwd", "passphrase",
    "lat", "latitude", "lon", "longitude", "geo", "location",
    "host", "hostname", "fqdn",
}
PRIVATE_IP_RE = re.compile(
    r"\b(?:10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+)\b"
)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
MAC_RE = re.compile(r"\b([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}\b")


def audit(text: str) -> dict:
    """Return a dict of audit findings — what would need scrubbing."""
    findings: dict = {
        "lines": 0,
        "private_ips": Counter(),
        "emails": set(),
        "jwts": set(),
        "macs": set(),
        "subtask_names": Counter(),
        "rtsp_urls": Counter(),
        "high_risk_kv": defaultdict(set),
        "ip_uint32_seen": Counter(),
    }
    for raw in text.splitlines():
        if not raw.strip():
            continue
        findings["lines"] += 1
        for m in PRIVATE_IP_RE.findall(raw):
            findings["private_ips"][m] += 1
        for m in EMAIL_RE.findall(raw):
            findings["emails"].add(m)
        for m in JWT_RE.findall(raw):
            findings["jwts"].add(m[:40] + "...")
        for m in MAC_RE.findall(raw):
            findings["macs"].add(m if isinstance(m, str) else m[0])
        try:
            evt = json.loads(raw)
        except json.JSONDecodeError:
            continue
        payload = evt.get("payload", {})
        _walk(payload, findings)
    return findings


def _walk(node, findings):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "subtask_name" and isinstance(v, str) and v:
                findings["subtask_names"][v] += 1
            if k == "rtsp_url" and isinstance(v, str) and v:
                findings["rtsp_urls"][v] += 1
            if k == "ip" and isinstance(v, int) and v not in (0, 0xFFFFFFFF):
                findings["ip_uint32_seen"][v] += 1
            if isinstance(k, str) and k.lower() in HIGH_RISK_KEYS and v not in (None, "", 0):
                findings["high_risk_kv"][k.lower()].add(repr(v)[:120])
            _walk(v, findings)
    elif isinstance(node, list):
        for v in node:
            _walk(v, findings)


def scrub(text: str) -> str:
    """Replace known-identifying values with synthetic ones."""
    out = text.replace(REAL_LAN_IP, SCRUB_LAN_IP)
    out = out.replace(str(REAL_LAN_IP_LE_UINT32), str(SCRUB_LAN_IP_LE_UINT32))
    out = out.replace(REAL_SUBTASK, SCRUB_SUBTASK)
    return out


def report(findings: dict) -> str:
    lines = []
    lines.append(f"== Audit of fixture ({findings['lines']} lines)")
    lines.append(f"  private_ips: {dict(findings['private_ips'])}")
    lines.append(f"  rtsp_urls: {dict(findings['rtsp_urls'])}")
    lines.append(f"  subtask_names: {dict(findings['subtask_names'])}")
    lines.append(f"  ip_uint32 (Bambu little-endian encoded LAN IP): "
                 f"{dict(findings['ip_uint32_seen'])}")
    lines.append(f"  emails: {sorted(findings['emails']) or '(none)'}")
    lines.append(f"  jwts: {sorted(findings['jwts']) or '(none)'}")
    lines.append(f"  macs: {sorted(findings['macs']) or '(none)'}")
    if findings["high_risk_kv"]:
        lines.append("  high-risk-key matches:")
        for k, vs in sorted(findings["high_risk_kv"].items()):
            lines.append(f"    {k}: {sorted(vs)[:5]}")
    else:
        lines.append("  high-risk-key matches: (none)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-only", action="store_true",
                        help="Print findings without writing the scrubbed file.")
    parser.add_argument("--write", action="store_true",
                        help="Write the scrubbed fixture to .demo.jsonl.")
    args = parser.parse_args(argv)

    if not args.audit_only and not args.write:
        parser.error("specify --audit-only or --write")

    if not SRC.exists():
        print(f"source fixture missing: {SRC}", file=sys.stderr)
        return 2

    src_text = SRC.read_text()
    src_findings = audit(src_text)
    print(report(src_findings))

    if not args.write:
        return 0

    scrubbed = scrub(src_text)
    DST.write_text(scrubbed)
    dst_findings = audit(scrubbed)

    print()
    print("== Post-scrub audit:")
    print(report(dst_findings))

    leak = (
        any(ip != SCRUB_LAN_IP for ip in dst_findings["private_ips"])
        or any(SCRUB_LAN_IP not in u for u in dst_findings["rtsp_urls"])
        or REAL_LAN_IP_LE_UINT32 in dst_findings["ip_uint32_seen"]
        or any(name == REAL_SUBTASK for name in dst_findings["subtask_names"])
        or dst_findings["emails"] or dst_findings["jwts"]
    )
    if leak:
        print("\nFAIL: scrub did not eliminate all known identifiers.", file=sys.stderr)
        return 1

    print(f"\nOK: wrote scrubbed fixture to {DST.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
