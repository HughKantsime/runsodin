"""Health probe for the demo.subsystem.app reviewer environment.

Asserts three invariants required by ODIN-128 verification:

  1. HTTPS GET on the demo login page returns 200.
  2. The mosquitto broker container is reachable on demo MQTT port.
  3. The demo publisher heartbeat is fresh (< freshness window).

Exit code 0 = green; 1 = degraded (a check failed). Stdout is structured
for shipping to ntfy / Prometheus / a manual paging hook by the caller.

Usage:
    python ops/demo/health_probe.py \\
        --base-url https://demo.subsystem.app \\
        --broker-host mosquitto-demo --broker-port 1883 \\
        --heartbeat-path /tmp/odin-demo-publisher.heartbeat \\
        --max-stale-sec 30

Per the issue, this probe is *only* paged during App Review windows
(manually flagged by ODIN QA each submission cycle). The pager-side
filter lives outside this script — this script just produces a green
or red line.
"""
from __future__ import annotations

import argparse
import json
import socket
import ssl
import sys
import time
import urllib.request
from pathlib import Path


def check_https(base_url: str, timeout: float) -> tuple[bool, str]:
    try:
        ctx = ssl.create_default_context()
        req = urllib.request.Request(f"{base_url.rstrip('/')}/login", method="GET")
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            ok = resp.status == 200
            return ok, f"https status={resp.status}"
    except Exception as exc:
        return False, f"https error: {exc}"


def check_broker(host: str, port: int, timeout: float) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"broker {host}:{port} reachable"
    except Exception as exc:
        return False, f"broker error: {exc}"


def check_heartbeat(path: Path, max_stale_sec: float) -> tuple[bool, str]:
    if not path.exists():
        return False, f"heartbeat missing: {path}"
    try:
        ts = float(path.read_text().strip())
    except (OSError, ValueError) as exc:
        return False, f"heartbeat unreadable: {exc}"
    age = time.time() - ts
    ok = age < max_stale_sec
    return ok, f"heartbeat age={age:.1f}s (max {max_stale_sec}s)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True,
                        help="https://demo.subsystem.app")
    parser.add_argument("--broker-host", default="mosquitto-demo")
    parser.add_argument("--broker-port", type=int, default=1883)
    parser.add_argument("--heartbeat-path", default="/tmp/odin-demo-publisher.heartbeat")
    parser.add_argument("--max-stale-sec", type=float, default=30.0)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--json", action="store_true",
                        help="Emit a single JSON line for ingestion.")
    args = parser.parse_args(argv)

    https_ok, https_msg = check_https(args.base_url, args.timeout)
    broker_ok, broker_msg = check_broker(args.broker_host, args.broker_port, args.timeout)
    hb_ok, hb_msg = check_heartbeat(Path(args.heartbeat_path), args.max_stale_sec)

    overall = https_ok and broker_ok and hb_ok
    result = {
        "ok": overall,
        "checks": {
            "https": {"ok": https_ok, "msg": https_msg},
            "broker": {"ok": broker_ok, "msg": broker_msg},
            "heartbeat": {"ok": hb_ok, "msg": hb_msg},
        },
        "ts": time.time(),
    }
    if args.json:
        print(json.dumps(result))
    else:
        status = "GREEN" if overall else "RED"
        print(f"{status}  https={https_ok} broker={broker_ok} heartbeat={hb_ok}")
        for name, c in result["checks"].items():
            print(f"  {name}: {c['msg']}")
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
