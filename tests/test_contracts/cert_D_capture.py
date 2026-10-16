"""cert-D producer-wire captures: compare by default, write only with CAPTURE=1.

Without CAPTURE=1 a capture never touches the working tree or $TMPDIR: it is
compared with the committed fixture and fails on producer drift: different keys,
list lengths, kinds (a field turning null fails), or values. Only keys in the
explicit VOLATILE_KEYS list (plus a call's ``volatile``) have their values
ignored -- timestamps, generated ids, sizes. With CAPTURE=1 the committed fixture
is rewritten (whole file, or one top-level key).
"""
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def capturing() -> bool:
    return os.environ.get("CAPTURE") == "1"


# Keys whose *values* legitimately differ between runs (timestamps, generated ids,
# sizes). Their kind -- including null versus non-null -- is still compared.
# Everything else (statuses, enums, names, counts, flags) must match exactly.
VOLATILE_KEYS = frozenset({
    "created_at", "updated_at", "timestamp", "actual_start", "actual_end", "loaded_at",
    "last_serviced", "next_due_date",
    "size_bytes", "size_mb",  # a SQLite file's size depends on what earlier tests left in it
})
ISO_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")


def mask_timestamps(text_value):
    """For a captured text body (e.g. a CSV export): mask embedded ISO timestamps only."""
    return ISO_TIMESTAMP.sub("<timestamp>", text_value)


def _kind(value):
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if value is None:
        return "null"
    return type(value).__name__


def drift(committed, produced, *, volatile=frozenset(), where="$"):
    """Every contract difference between two JSON values (empty list: none)."""
    volatile = VOLATILE_KEYS | frozenset(volatile)
    found = []

    def walk(a, b, path, key):
        if isinstance(a, dict) and isinstance(b, dict):
            if set(a) != set(b):
                found.append(f"{path}: keys differ (missing {sorted(set(a) - set(b))}, new {sorted(set(b) - set(a))})")
            for name in sorted(set(a) & set(b)):
                walk(a[name], b[name], f"{path}.{name}", name)
            return
        if isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b):
                found.append(f"{path}: {len(a)} items committed, {len(b)} produced")
            for index, (x, y) in enumerate(zip(a, b)):
                walk(x, y, f"{path}[{index}]", key)
            return
        if _kind(a) != _kind(b):
            found.append(f"{path}: {_kind(a)} committed, {_kind(b)} produced")
        elif key not in volatile and a != b:
            found.append(f"{path}: {a!r} committed, {b!r} produced")

    walk(committed, produced, where, None)
    return found


def publish(repo_path, value, *, key=None, indent=2, volatile=(), normalize=None):
    """Compare (default) or, with CAPTURE=1, write a capture into the committed fixture.

    ``volatile`` names further keys (beyond VOLATILE_KEYS) whose values may vary;
    ``normalize`` (applied to both sides) masks volatile parts of a non-JSON body.
    """
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
    if normalize is not None:
        expected, produced = normalize(expected), normalize(produced)
    differences = drift(expected, produced, volatile=volatile)
    assert not differences, (f"Producer contract drift in {repo_path.name}{'' if key is None else ':' + key}: "
                             + "; ".join(differences[:20]) + " (re-capture with CAPTURE=1 after reconciling the consumer)")
