"""Static census of filament stock writers (per call site), for test_cert_D_filament_ledger."""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
LEDGER = "backend/core/filament_ledger.py"
SKIP_PARTS = ("preserved027", "/migrations/", "__pycache__")
SKIP_FILES = ("backend/scripts/seed_",)

PATTERNS = {
    "insert spool_usage": re.compile(r"(?i)insert\s+(?:or\s+\w+\s+)?into\s+[\"'`]?spool_usage\b"),
    "SpoolUsage()": re.compile(r"\bSpoolUsage\s*\("),
    "weight_used_g write": re.compile(r"\bweight_used_g\b[\"'`]?\s*(?:[-+]=|=(?!=))"),
    "remaining_weight_g write": re.compile(r"\bremaining_weight_g\b[\"'`]?\s*(?:[-+]=|=(?!=))"),
    "setattr on a spool": re.compile(r"setattr\(\s*\w*spool\w*\s*,", re.I),
    "ORM bulk update of spools": re.compile(
        r"query\(\s*Spool\s*\)[^\n]*\.update\(|Spool\.__table__\.update|\b(?:sa_)?update\(\s*Spool\s*\)"
        r"|bulk_update_mappings\(\s*Spool\b|(?i:update\s+[\"'`]?spools[\"'`]?\s+(?:as\s+)?\w*\s*set\b[^\n]*remaining)"),
    "spool_usage via table object": re.compile(r"SpoolUsage\.__table__\.insert|insert\(\s*SpoolUsage\s*\)"),
}
NOT_A_WRITE = re.compile(r"=\s*Column\(|:\s*(?:Optional\[)?float|^\s*#|^\s*class\s")


def _functions(tree):
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            spans.append((node.lineno, node.end_lineno, node.name))
    return spans


def _enclosing(spans, line):
    inside = [s for s in spans if s[0] <= line <= s[1]]
    return min(inside, key=lambda s: s[1] - s[0])[2] if inside else "<module>"


def _sources():
    for path in sorted(BACKEND.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel == LEDGER or any(p in "/" + rel for p in SKIP_PARTS) or rel.startswith(SKIP_FILES):
            continue
        yield rel, path.read_text(encoding="utf-8")


def census(sources=None):
    """{(path, function, pattern): count} for every writer outside the ledger."""
    found = {}
    for rel, source in (sources.items() if sources is not None else _sources()):
        if not any(p.search(source) for p in PATTERNS.values()):
            continue
        spans = _functions(ast.parse(source))
        for number, line in enumerate(source.splitlines(), start=1):
            if NOT_A_WRITE.search(line):
                continue
            for name, pattern in PATTERNS.items():
                if pattern.search(line):
                    key = (rel, _enclosing(spans, number), name)
                    found[key] = found.get(key, 0) + 1
    return found
