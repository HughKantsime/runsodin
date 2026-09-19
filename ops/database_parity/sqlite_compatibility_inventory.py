"""Generate and verify ODIN's classified runtime SQLite compatibility inventory."""

from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path


ROOT = Path(__file__).parents[2]
BACKEND = ROOT / "backend"
INVENTORY = ROOT / "ops" / "database_parity" / "sqlite_compatibility_inventory.json"


CLASSIFICATIONS: dict[str, tuple[str, str]] = {
    "backend/core/app.py": (
        "dialect-neutral",
        "Runtime SQL executes through the dialect-aware shared DBAPI compatibility adapter",
    ),
    "backend/core/database_config.py": (
        "sqlite-provider-only",
        "SQLite engine branch; PostgreSQL engine creation does not receive SQLite connect args",
    ),
    "backend/core/db.py": (
        "sqlite-provider-only",
        "SQLite connection event hooks guarded by the configured provider",
    ),
    "backend/core/db_compat.py": (
        "dialect-neutral",
        "Provider abstraction translates qmark SQL and normalizes insert identity semantics",
    ),
    "backend/core/db_utils.py": (
        "dialect-neutral",
        "Shared DBAPI adapter conditionally applies SQLite PRAGMAs and adapts PostgreSQL SQL",
    ),
    "backend/core/schema/migrator.py": (
        "sqlite-provider-only",
        "Dedicated SQLite branches use PRAGMA for schema inspection, migration FK toggling, and pre-commit integrity verification",
    ),
    "backend/core/schema/migrations/002_education_tenant_integrity.py": (
        "sqlite-provider-only",
        "Education migration uses guarded SQLite PRAGMA inspection while PostgreSQL uses SQLAlchemy reflection",
    ),
    "backend/modules/organizations/education_policy_inventory.py": (
        "dialect-neutral",
        "Static source-discovery regex contains SQL vocabulary and regex question marks; it executes no database statement",
    ),
    "backend/modules/organizations/education_policy.py": (
        "dialect-neutral",
        "Central policy translates named parameters for the shared DBAPI adapter and uses sqlite3 only to classify integrity exceptions",
    ),
    "backend/modules/system/backup_service.py": (
        "sqlite-provider-only",
        "Dedicated SQLite backup, validation, and offline restore provider",
    ),
    "backend/modules/system/routes_admin.py": (
        "sqlite-provider-only",
        "SQLite integrity-check branch; PostgreSQL uses provider-specific health checks",
    ),
    "backend/modules/archives/archive.py": (
        "dialect-neutral",
        "Insert identity is supplied by the dialect-aware DBAPI result adapter",
    ),
    "backend/modules/notifications/channels.py": (
        "dialect-neutral",
        "Qmark statements execute through the dialect-aware shared DBAPI adapter",
    ),
    "backend/modules/notifications/error_handling.py": (
        "dialect-neutral",
        "Qmark statements execute through the dialect-aware shared DBAPI adapter",
    ),
    "backend/modules/notifications/job_events.py": (
        "dialect-neutral",
        "Qmark and insert identity paths use the dialect-aware shared DBAPI adapter",
    ),
    "backend/modules/notifications/printer_health.py": (
        "dialect-neutral",
        "Qmark statements execute through the dialect-aware shared DBAPI adapter",
    ),
    "backend/modules/printers/monitors/mqtt_job_lifecycle.py": (
        "dialect-neutral",
        "Qmark and insert identity paths use the dialect-aware shared DBAPI adapter",
    ),
    "backend/modules/printers/monitors/mqtt_printer.py": (
        "dialect-neutral",
        "Qmark statements execute through the dialect-aware shared DBAPI adapter",
    ),
    "backend/scripts/demo_seed_edu.py": (
        "sqlite-provider-only",
        "SQLite-only local EDU demo seed command",
    ),
    "backend/scripts/demo_seed_reviewer.py": (
        "sqlite-provider-only",
        "SQLite-only local reviewer demo seed command",
    ),
    "backend/scripts/seed_release_gate.py": (
        "dialect-neutral",
        "Candidate seed uses a dual-provider connection shim and provider-specific schema inspection",
    ),
    "backend/scripts/seed_edu_sandbox.py": (
        "dialect-neutral",
        "Education sandbox seed uses the dual-provider connection shim and provider-specific insert identity",
    ),
}


def _call_name(node: ast.Call) -> str:
    function = node.func
    return function.attr if isinstance(function, ast.Attribute) else ""


def _snippet(source_lines: list[str], lineno: int) -> str:
    return source_lines[lineno - 1].strip()[:240]


def _is_qmark_sql_fragment(value: str) -> bool:
    if "?" not in value:
        return False
    if re.search(r"\b(?:SELECT|INSERT|UPDATE|DELETE|VALUES|WHERE|SET)\b", value, re.I):
        return True
    return bool(
        re.search(
            r"=\s*\?(?=\s*[,)]|$)|\?\s*[,)]|[(,]\s*\?(?=\s*[,)]|$)",
            value,
        )
    )


def scan_source(relative: str, source: str) -> list[dict[str, object]]:
    """Return classified compatibility sites for one repository-relative source."""
    lines = source.splitlines()
    tree = ast.parse(source, filename=relative)
    docstrings = {
        id(owner.body[0].value)
        for owner in ast.walk(tree)
        if isinstance(owner, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and owner.body
        and isinstance(owner.body[0], ast.Expr)
        and isinstance(owner.body[0].value, ast.Constant)
        and isinstance(owner.body[0].value.value, str)
    }
    joined_string_members = {
        id(member)
        for joined in ast.walk(tree)
        if isinstance(joined, ast.JoinedStr)
        for member in ast.walk(joined)
        if member is not joined
    }
    hits: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            alias.name == "sqlite3" for alias in node.names
        ):
            hits.add((node.lineno, "sqlite3"))
        elif isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
            hits.add((node.lineno, "sqlite3"))
        elif isinstance(node, ast.keyword) and node.arg == "check_same_thread":
            hits.add((node.lineno, "check_same_thread"))
        elif isinstance(node, ast.Attribute) and node.attr == "lastrowid":
            hits.add((node.lineno, "lastrowid"))
        elif isinstance(node, ast.JoinedStr):
            literal_text = "".join(
                member.value
                for member in node.values
                if isinstance(member, ast.Constant) and isinstance(member.value, str)
            )
            if _is_qmark_sql_fragment(literal_text):
                hits.add((node.lineno, "qmark-parameter"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            upper = node.value.upper()
            if id(node) not in docstrings and node.value == "check_same_thread":
                # SQLAlchemy supplies this SQLite-only option inside connect_args,
                # where it is represented as a dictionary key rather than a call
                # keyword argument.
                hits.add((node.lineno, "check_same_thread"))
            if "PRAGMA" in upper:
                hits.add((node.lineno, "PRAGMA"))
            if "LAST_INSERT_ROWID" in upper:
                hits.add((node.lineno, "last_insert_rowid"))
            if id(node) not in docstrings and id(node) not in joined_string_members and (
                _is_qmark_sql_fragment(node.value)
                or (
                    relative == "backend/core/db_utils.py"
                    and node.value.strip() == "?"
                )
            ):
                hits.add((node.lineno, "qmark-parameter"))
        if isinstance(node, ast.Call) and _call_name(node) in {"execute", "executemany"}:
            if node.args:
                # Walk the complete SQL-expression tree, not only a direct string
                # constant. This catches f-strings, concatenated fragments, and
                # generated placeholder lists such as ','.join('?' * len(ids)).
                for fragment in ast.walk(node.args[0]):
                    if isinstance(fragment, ast.JoinedStr):
                        literal_text = "".join(
                            member.value
                            for member in fragment.values
                            if isinstance(member, ast.Constant)
                            and isinstance(member.value, str)
                        )
                        if "?" in literal_text:
                            hits.add((fragment.lineno, "qmark-parameter"))
                    elif (
                        isinstance(fragment, ast.Constant)
                        and isinstance(fragment.value, str)
                        and "?" in fragment.value
                        and id(fragment) not in joined_string_members
                    ):
                        hits.add((fragment.lineno, "qmark-parameter"))

    classification = CLASSIFICATIONS.get(relative)
    if hits and classification is None:
        raise RuntimeError(f"unclassified SQLite compatibility site: {relative}")
    if not classification:
        return []
    label, reason = classification
    return [
        {
            "path": relative,
            "line": lineno,
            "pattern": pattern,
            "classification": label,
            "reason": reason,
            "source": _snippet(lines, lineno),
        }
        for lineno, pattern in sorted(hits)
    ]


def scan_file(path: Path) -> list[dict[str, object]]:
    relative = path.relative_to(ROOT).as_posix()
    return scan_source(relative, path.read_text(encoding="utf-8"))


def generate() -> dict[str, object]:
    entries: list[dict[str, object]] = []
    for path in sorted(BACKEND.rglob("*.py")):
        entries.extend(scan_file(path))
    return {
        "schema_version": 1,
        "patterns": [
            "sqlite3",
            "PRAGMA",
            "check_same_thread",
            "qmark-parameter",
            "last_insert_rowid",
            "lastrowid",
        ],
        "entries": entries,
    }


def serialized_inventory() -> str:
    return json.dumps(generate(), indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    generated = serialized_inventory()
    if args.write:
        INVENTORY.write_text(generated, encoding="utf-8")
        print(f"wrote {INVENTORY.relative_to(ROOT)}")
        return 0
    if not INVENTORY.is_file() or INVENTORY.read_text(encoding="utf-8") != generated:
        raise SystemExit(
            "SQLite compatibility inventory is stale; review changes and run "
            "python3 -m ops.database_parity.sqlite_compatibility_inventory --write"
        )
    print(f"SQLite compatibility inventory PASS: {len(generate()['entries'])} sites")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
