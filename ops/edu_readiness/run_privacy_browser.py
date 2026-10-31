"""Serve the compiled frontend and run the browser privacy lifecycle."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

try:
    from .preview_server import PreviewError, local_address, owned_preview
except ImportError:
    from preview_server import PreviewError, local_address, owned_preview


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    output = args.run_dir / "raw" / "privacy-browser.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    base_url = os.environ.get("EDU_FRONTEND_URL", "http://127.0.0.1:4173")
    local_address(base_url)

    build = subprocess.run(["npm", "--prefix", "frontend", "run", "build"], check=False)
    if build.returncode:
        return build.returncode
    env = dict(os.environ, EDU_FRONTEND_URL=base_url, EDU_PRIVACY_BROWSER_OUTPUT=str(output))
    try:
        with owned_preview(base_url):
            return subprocess.run(["node", "tests/privacy/privacy_browser_audit.mjs"], env=env, check=False).returncode
    except PreviewError as exc:
        print(f"preview: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
