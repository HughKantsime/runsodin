"""The root .semgrepignore may exclude live sources only when they are provably byte-pinned.

Semgrep findings are normally resolved by fixing the code or annotating the exact
line. A live file whose exact bytes are pinned by SHA-256 cannot take an inline
annotation, so it may instead be listed in .semgrepignore. This contract keeps that
list grow-only-by-proof: every listed live file must be pinned at its current bytes
by a real pin source, recomputed here from those sources:

* the migrator checksum registry (_ALLOWED_PYTHON_MIGRATIONS; checksums recorded
  in odin_schema_migrations on every deployed database),
* each schema migration's helper pins (SOURCE_PINS, SOURCE_SHA256, HELPER_SHA256),
* the restore-contract SHA tables (pre026_restore_contract SOURCE_PINS and
  EXTRA026_SOURCE_PINS, prepriced_restore_contract PROFILE_JSON sources),
* the certification fixtures (prepriced027 catalogue, pre026 history oracle,
  committed-completion-boundary source-current.json).

It also keeps semgrep's built-in default ignore list intact (a .semgrepignore
replaces it) and the frozen-bundle exclusion limited to the explicitly pinned directories.
"""
import ast
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
MIGRATIONS = BACKEND / "core" / "schema" / "migrations"
FIXTURES = ROOT / "tests" / "fixtures" / "certification"

# semgrep 1.163.0 built-in defaults (Semgrepignore.ml), replaced by any .semgrepignore.
DEFAULT_PATTERNS = (
    ".git", ".svn", "_darcs", "build/", "vendor/", "dist/", "*.min.js", ".env/", ".tox/",
    "node_modules/", ".npm/", ".yarn/", ".venv/", "_opam/", "_build/", "_cargo/",
    "test/", "tests/", "testsuite/", "*_test.go",
)
BUNDLES = ("/backend/core/schema/preserved027/bundle/", "/backend/core/schema/preserved029/bundle/", "/backend/core/schema/preserved034/bundle/")


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assignments(path):
    """Module-level NAME = <literal> assignments, evaluated without importing."""
    values = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                values[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                pass
    return values


def _allowed_python_migrations():
    for node in ast.parse((BACKEND / "core" / "schema" / "migrator.py").read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "_ALLOWED_PYTHON_MIGRATIONS":
            call = node.value
            assert isinstance(call, ast.Call) and call.func.id == "frozenset"
            return set(ast.literal_eval(call.args[0]))
    raise AssertionError("migrator registry _ALLOWED_PYTHON_MIGRATIONS not found")


def pinned_sources():
    """Map repo-relative live file -> pin sources that pin its current bytes."""
    proofs = {}

    def prove(path, digest, source):
        path = path.resolve()
        if path.is_file() and _sha(path) == digest:
            proofs.setdefault(path.relative_to(ROOT).as_posix(), set()).add(source)

    # Migrator checksum registry: the checksum lives in each deployed ledger, so
    # registry membership of the exact module file is the proof.
    for module in _allowed_python_migrations():
        assert module.startswith("core.schema.migrations.")
        path = MIGRATIONS / (module.rsplit(".", 1)[1] + ".py")
        assert path.is_file(), module
        proofs.setdefault(path.relative_to(ROOT).as_posix(), set()).add("migrator checksum")

    # Each migration's helper pins (SOURCE_PINS, SOURCE_SHA256, HELPER_SHA256,
    # SCHEMA_SOURCE_SHA256 ...): the migration names the helper module and
    # carries a SHA-256 literal equal to the helper's current bytes.
    live = [path for path in BACKEND.rglob("*.py") if "/bundle/" not in path.as_posix()]
    digests = {}
    for path in live:
        digests.setdefault(_sha(path), []).append(path)
    for migration in sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.py")):
        source = migration.read_text(encoding="utf-8")
        for digest in set(re.findall(r"'([0-9a-f]{64})'", source)):
            for path in digests.get(digest, ()):
                if path != migration and re.search(r"\b" + re.escape(path.stem) + r"\b", source):
                    prove(path, digest, f"migration {migration.stem} helper pin")

    # Restore-contract SHA tables (paths relative to backend/).
    contract = _assignments(BACKEND / "core" / "schema" / "pre026_restore_contract.py")
    for table in ("SOURCE_PINS", "EXTRA026_SOURCE_PINS"):
        assert contract.get(table), table
        for name, digest in contract[table].items():
            prove(BACKEND / name, digest, f"pre026_restore_contract.py {table}")
    profile = json.loads(_assignments(BACKEND / "core" / "schema" / "prepriced_restore_contract.py")["PROFILE_JSON"])
    for name, digest in profile["sources"].items():
        prove(BACKEND / name, digest, "prepriced_restore_contract.py PROFILE_JSON sources")

    # Certification fixtures.
    for name, digest in json.loads((FIXTURES / "prepriced027" / "catalogue.json").read_text())["sources"].items():
        prove(BACKEND / name, digest, "prepriced027 catalogue.json")
    oracle = json.loads((FIXTURES / "pre026-independent-history-oracle.json").read_text())
    for name, digest, _dialect in oracle["expected_postgres_ledger"]:
        prove(BACKEND / name, digest, "pre026 history oracle")
    boundary = json.loads((FIXTURES / "committed-completion-boundary" / "source-current.json").read_text())
    target = re.search(r"\b(backend/[\w/]+\.py)\b", boundary["note"])
    assert target
    prove(ROOT / target.group(1), boundary["job_events_sha256"], "committed-completion-boundary source-current.json")
    return proofs


def _entries(text):
    """(pattern, preceding comment) for every pattern line of a .semgrepignore."""
    entries, comment = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            comment = None
        elif line.startswith("#"):
            comment = line
        else:
            entries.append((line, comment))
            comment = None
    return entries


def unproven_live_exclusions(text, proofs):
    """Live-file patterns in a .semgrepignore that lack a pin proof or a pin comment."""
    problems = []
    for pattern, comment in _entries(text):
        if pattern in DEFAULT_PATTERNS or pattern in BUNDLES:
            continue
        relative = pattern[1:] if pattern.startswith("/") else None
        if (relative is None or any(ch in relative for ch in "*?[!") or not (ROOT / relative).is_file()
                or relative not in proofs or not comment):
            problems.append(pattern)
    return problems


def test_live_exclusions_are_exactly_hash_pinned_files():
    text = (ROOT / ".semgrepignore").read_text(encoding="utf-8")
    proofs = pinned_sources()
    assert unproven_live_exclusions(text, proofs) == []
    live = [p for p, _ in _entries(text) if p not in DEFAULT_PATTERNS and p not in BUNDLES]
    assert len(live) == len(set(live))
    assert live, "expected the byte-pinned live sources to be listed"


def test_defaults_are_reproduced_and_bundles_are_the_only_directories():
    patterns = [p for p, _ in _entries((ROOT / ".semgrepignore").read_text(encoding="utf-8"))]
    assert [p for p in patterns if p in DEFAULT_PATTERNS] == list(DEFAULT_PATTERNS)
    assert [p for p in patterns if p.endswith("/") and p not in DEFAULT_PATTERNS] == list(BUNDLES)
    for bundle in BUNDLES:
        assert (ROOT / bundle.strip("/")).is_dir()


def test_pin_sources_are_read_from_every_kind_of_pin():
    kinds = {source.split(" ")[0] for sources in pinned_sources().values() for source in sources}
    assert {"migrator", "migration", "pre026_restore_contract.py", "prepriced_restore_contract.py",
            "prepriced027", "pre026", "committed-completion-boundary"} <= kinds


def test_an_unpinned_or_uncommented_live_file_is_refused():
    proofs = pinned_sources()
    unpinned = "/" + Path(__file__).resolve().relative_to(ROOT).as_posix()
    pinned = "/" + sorted(path for path in proofs if path.startswith("backend/"))[0]
    text = "\n".join(DEFAULT_PATTERNS + BUNDLES) + f"\n# a pin\n{unpinned}\n\n{pinned}\n# a pin\n/backend/missing.py\n# a pin\n/backend/core/*.py\n"
    assert unproven_live_exclusions(text, proofs) == [unpinned, pinned, "/backend/missing.py", "/backend/core/*.py"]
