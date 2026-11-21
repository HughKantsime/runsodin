"""Check the PostgreSQL tools installed in a built ODIN image."""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


MINIMUMS = Path(__file__).with_name("image_client_minimums.json")


def check_image_client(image, runner=subprocess.run):
    minimums = json.loads(MINIMUMS.read_text(encoding="utf-8"))
    versions = {}
    for tool in ("pg_restore", "pg_dump"):
        required = minimums[tool]
        try:
            result = runner(
                ["docker", "run", "--rm", "--entrypoint", tool, image, "--version"],
                capture_output=True, text=True, errors="replace", timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError(f"{tool} could not run. Version {required} or later is required.") from exc
        if result.returncode:
            raise ValueError(f"{tool} could not run (exit {result.returncode}). Version {required} or later is required.")
        match = re.fullmatch(
            rf"{tool} \(PostgreSQL\) ([0-9]+\.[0-9]+)(?: \([^()\r\n]+\))?",
            result.stdout.strip(),
        )
        if not match:
            raise ValueError(f"{tool} returned an unreadable version. Version {required} or later is required.")
        found = match[1]
        if tuple(map(int, found.split("."))) < tuple(map(int, required.split("."))):
            raise ValueError(f"Found {tool} {found}. Version {required} or later is required.")
        versions[tool] = found
    print(json.dumps({"image": image, **versions, "ok": True}), flush=True)


def main(argv=None, runner=subprocess.run):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    args = parser.parse_args(argv)
    try:
        check_image_client(args.image, runner)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
