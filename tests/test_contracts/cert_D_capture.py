"""cert-D producer-wire captures: compare by default, write only with CAPTURE=1.

Without CAPTURE=1 a capture never touches the working tree or $TMPDIR: it is
compared with the committed fixture and fails on producer drift. Drift means a
different contract shape (keys, list lengths, value types) - volatile scalar
values such as timestamps or generated ids are not compared. With CAPTURE=1 the
committed fixture is rewritten (whole file, or one top-level key).
"""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def capturing() -> bool:
    return os.environ.get("CAPTURE") == "1"


def _kind(value):
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if value is None:
        return "null"
    return type(value).__name__


def shape_drift(committed, produced, where="$"):
    """Return the first contract difference between two JSON values, or None."""
    if isinstance(committed, dict) and isinstance(produced, dict):
        if set(committed) != set(produced):
            return f"{where}: keys differ (missing {sorted(set(committed) - set(produced))}, new {sorted(set(produced) - set(committed))})"
        for key in committed:
            found = shape_drift(committed[key], produced[key], f"{where}.{key}")
            if found:
                return found
        return None
    if isinstance(committed, list) and isinstance(produced, list):
        if len(committed) != len(produced):
            return f"{where}: {len(committed)} items committed, {len(produced)} produced"
        for index, (a, b) in enumerate(zip(committed, produced)):
            found = shape_drift(a, b, f"{where}[{index}]")
            if found:
                return found
        return None
    a, b = _kind(committed), _kind(produced)
    if a != b and "null" not in (a, b):  # optional fields may legitimately be null in one capture
        return f"{where}: {a} committed, {b} produced"
    return None


def publish(repo_path, value, *, key=None, indent=2):
    """Compare (default) or, with CAPTURE=1, write a capture into the committed fixture."""
    repo_path = Path(repo_path)
    committed = json.loads(repo_path.read_text()) if repo_path.exists() else None
    if capturing():
        data = value if key is None else {**(committed or {}), key: value}
        repo_path.parent.mkdir(parents=True, exist_ok=True)
        repo_path.write_text(json.dumps(data, indent=indent, sort_keys=True) + "\n")
        return
    assert committed is not None, f"{repo_path.name} is missing; capture it with CAPTURE=1"
    expected = committed if key is None else committed.get(key)
    assert expected is not None or key is None, f"{repo_path.name}:{key} is not committed; capture it with CAPTURE=1"
    produced = json.loads(json.dumps(value))
    drift = shape_drift(expected, produced)
    assert drift is None, f"Producer contract drift in {repo_path.name}{'' if key is None else ':' + key}: {drift} (re-capture with CAPTURE=1 after reconciling the consumer)"
