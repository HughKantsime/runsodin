"""Where cert-D producer-wire captures are written.

Captures overwrite the committed frontend fixtures only when CAPTURE=1 is set.
Otherwise they go to a per-run temp mirror, seeded from the committed file so
compare-and-merge captures still check against the reviewed fixture, and an
ordinary test run never dirties the working tree.
"""
import os
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_MIRROR = Path(tempfile.gettempdir()) / f"odin-certD-capture-{os.getpid()}"


def capturing() -> bool:
    return os.environ.get("CAPTURE") == "1"


def capture_path(repo_path) -> Path:
    repo_path = Path(repo_path).resolve()
    if capturing():
        return repo_path
    target = _MIRROR / repo_path.relative_to(ROOT)
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        if repo_path.exists():
            shutil.copyfile(repo_path, target)
    return target
