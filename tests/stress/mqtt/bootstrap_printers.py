"""POST N synthetic printers to a running ODIN backend so its MQTT subscriber
binds against the topics the multi_publisher will be writing.

Mirrors `scripts/capture/odin_capture/processes._post_printer` for auth — same
X-API-Key + Authorization pattern. Idempotent: 400 "already exists" is a
success.

Run after `docker compose -f ops/demo/docker-compose.demo.yml up -d` and
before `multi_publisher`.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from .fixture_multiplier import synthetic_serial

log = logging.getLogger("stress.bootstrap")


def _post_printer(
    base_url: str,
    body: dict,
    *,
    api_key: str,
    access_token: str,
    timeout: float,
) -> tuple[bool, str]:
    url = f"{base_url.rstrip('/')}/api/printers"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
            "Authorization": f"Bearer {access_token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if 200 <= resp.status < 300:
                return True, f"{resp.status}"
            return False, f"unexpected {resp.status}"
    except urllib.error.HTTPError as e:
        envelope = e.read().decode("utf-8", "replace")
        if e.code == 400 and "already exists" in envelope.lower():
            return True, "exists"
        return False, f"HTTP {e.code} {envelope[:200]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed N synthetic printers")
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--api-host", default="mosquitto", help="Must match broker_policy allowlist")
    parser.add_argument("--model", default="X1C")
    parser.add_argument("--name-prefix", default="stress")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--out", type=Path, default=Path("stress-out/bootstrap.json"))
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    api_key = os.environ.get("ODIN_DEMO_API_KEY") or os.environ.get("API_KEY") or ""
    if not api_key:
        log.error("Set ODIN_DEMO_API_KEY (or API_KEY) before running")
        return 2
    access_token = os.environ.get("ODIN_ACCESS_TOKEN", "")

    results: list[dict] = []
    ok_count = 0
    started = time.time()
    for i in range(args.count):
        serial = synthetic_serial(i)
        body = {
            "name": f"{args.name_prefix}-{i:04d}",
            "model": args.model,
            "api_type": "bambu",
            "api_host": args.api_host,
            "api_key": f"{serial}|stress-placeholder",
            "slot_count": 4,
            "is_active": True,
        }
        ok, msg = _post_printer(
            args.base_url,
            body,
            api_key=api_key,
            access_token=access_token,
            timeout=args.timeout,
        )
        results.append({"index": i, "serial": serial, "ok": ok, "msg": msg})
        if ok:
            ok_count += 1
        else:
            log.warning("printer %d (%s) failed: %s", i, serial, msg)

    elapsed = time.time() - started
    summary = {
        "requested": args.count,
        "succeeded": ok_count,
        "failed": args.count - ok_count,
        "elapsed_s": round(elapsed, 3),
        "base_url": args.base_url,
        "results": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2))
    log.info(
        "seeded %d/%d printers in %.2fs — wrote %s",
        ok_count, args.count, elapsed, args.out,
    )
    return 0 if ok_count == args.count else 1


if __name__ == "__main__":
    sys.exit(main())
