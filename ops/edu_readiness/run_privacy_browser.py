"""Serve the compiled frontend and run the browser privacy lifecycle."""

from __future__ import annotations

import argparse
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    output = args.run_dir / "raw" / "privacy-browser.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    base_url = os.environ.get("EDU_FRONTEND_URL", "http://127.0.0.1:4173")
    parsed = urlsplit(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("EDU_FRONTEND_URL must be a local HTTP preview URL")

    build = subprocess.run(["npm", "--prefix", "frontend", "run", "build"], check=False)
    if build.returncode:
        return build.returncode
    preview = None
    try:
        try:
            urllib.request.urlopen(base_url, timeout=1).close()  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- URL constrained above
        except Exception:
            preview = subprocess.Popen(
                ["npm", "--prefix", "frontend", "run", "preview", "--", "--host", "127.0.0.1"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            for _ in range(50):
                try:
                    urllib.request.urlopen(base_url, timeout=1).close()  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- URL constrained above
                    break
                except Exception:
                    time.sleep(0.1)
            else:
                raise RuntimeError("Vite preview did not become ready")
        env = dict(os.environ, EDU_FRONTEND_URL=base_url, EDU_PRIVACY_BROWSER_OUTPUT=str(output))
        return subprocess.run(["node", "tests/privacy/privacy_browser_audit.mjs"], env=env, check=False).returncode
    finally:
        if preview is not None:
            preview.terminate()
            try:
                preview.wait(timeout=5)
            except subprocess.TimeoutExpired:
                preview.kill()


if __name__ == "__main__":
    raise SystemExit(main())
