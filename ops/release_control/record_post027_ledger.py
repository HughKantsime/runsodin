"""Record the post-027 migration ids of the version about to be published.

Usage: python ops/release_control/record_post027_ledger.py [--version X.Y.Z]
Reads VERSION by default and appends ``version -> POST027_MIGRATIONS ids`` to
backend/core/schema/released_post027_ledgers.json. Commit the result into the release
candidate; publish-image.yml refuses a VERSION without a matching entry. An existing
entry is never rewritten.
"""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
LEDGERS = Path("backend/core/schema/released_post027_ledgers.json")
BOOTSTRAP = Path("backend/core/schema/bootstrap.py")


def registered_ids(root: Path) -> list[str]:
    tree = ast.parse((root / BOOTSTRAP).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and [getattr(t, "id", None) for t in node.targets] == ["POST027_MIGRATIONS"]:
            return [migration_id for migration_id, _module in ast.literal_eval(node.value)]
    raise SystemExit("POST027_MIGRATIONS literal not found")


def record(root: Path, version: str) -> list[str]:
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version):
        raise SystemExit("version must be X.Y.Z")
    path = root / LEDGERS
    if not path.is_file():
        raise SystemExit(f"{LEDGERS} is missing; restore it from the release history before recording {version}")
    document = json.loads(path.read_text(encoding="utf-8"))
    current = registered_ids(root)
    existing = document["releases"].get(version)
    if existing is not None:
        if existing != current:
            raise SystemExit(f"{version} is already recorded with {existing}; it cannot be rewritten to {current}")
        return current
    document["releases"][version] = current
    path.write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8")
    return current


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    version = args.version or (args.root / "VERSION").read_text(encoding="utf-8").strip()
    print(json.dumps({"version": version, "post027": record(args.root, version)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
