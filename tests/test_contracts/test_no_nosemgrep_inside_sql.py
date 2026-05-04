"""
Contract test — `# nosemgrep` directives must not land inside SQL strings.

Guards against the regression introduced in 31d6f0d ("security: triage 125
semgrep findings"), where this antipattern slipped in across 18 sites:

    session.execute(text(f\"\"\"  # nosemgrep: <rule> -- <reason>
        SELECT ...
    \"\"\"))

Inside a triple-quoted f-string, `# nosemgrep:...` is part of the SQL.
SQLite chokes on the `#` token (`unrecognized token: "#"`) and the
query fails at runtime — silently in debug-suppressed code paths,
loudly in alert dispatch (every HMS error spams the logs).

Caused 16+ days of broken alert delivery in prod before being noticed
during V2-flip pre-flip diagnosis.

Rule: any string passed to a `text(...)` call or to `cursor.execute(...)`
must not contain a line beginning with `#` (after leading whitespace).
The `# nosemgrep:` directive belongs on the LINE BEFORE the call —
semgrep applies suppressions to the next line, so the meaning is
preserved without polluting the SQL string.

Run: pytest tests/test_contracts/test_no_nosemgrep_inside_sql.py -v
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _string_value(node: ast.AST) -> str | None:
    """Return the literal/joined string content of a string-like node, or
    None if it can't be statically resolved."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        # f-string — inspect each part, treating FormattedValue as a
        # placeholder that doesn't itself introduce a `#` line.
        out: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                out.append(value.value)
            else:
                out.append('{}')
        return ''.join(out)
    return None


def _is_sql_call(node: ast.AST) -> bool:
    """True if this Call node is `text(...)`, `*.execute(...)`,
    `*.executemany(...)`, or `*.executescript(...)`."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name) and func.id == 'text':
        return True
    if isinstance(func, ast.Attribute) and func.attr in {
        'execute', 'executemany', 'executescript'
    }:
        return True
    return False


def _has_hash_comment_line(s: str) -> bool:
    """True if the string contains the broken-directive antipattern.

    Two regression vectors, both originating from the same root cause
    (commit 31d6f0d): the `# nosemgrep:` directive ended up inside the
    SQL because `text(f\"\"\"<sql>  # nosemgrep:...` puts the comment
    inside the f-string.

    Vector A — start-of-line `#`: the f-string opens immediately with
    `\"\"\"  # nosemgrep:...`. The first line of the SQL string starts
    with `#`.

    Vector B — mid-line `#`: the f-string opens with content on the
    same line as the call, like `\"\"\"<sql content>  # nosemgrep:...`.
    The substring `# nosemgrep:` appears mid-string.

    Both are bugs. We catch BOTH by looking for the literal substring
    `# nosemgrep` anywhere in the string AND for any line starting with
    `#`. Real SQL never contains `# nosemgrep` and never starts a line
    with `#` — both checks are precise."""
    if '# nosemgrep' in s:
        return True
    for raw_line in s.splitlines():
        if raw_line.lstrip().startswith('#'):
            return True
    return False


def _find_violations(py_file: Path) -> list[tuple[int, str]]:
    """Return [(lineno, snippet)] for every SQL-call argument that
    contains a `#`-prefixed line."""
    try:
        source = py_file.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(source, filename=str(py_file))
    except SyntaxError:
        return []

    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not _is_sql_call(node):
            continue
        if not node.args:
            continue
        first = node.args[0]
        s = _string_value(first)
        if s is None:
            continue
        if _has_hash_comment_line(s):
            offending = next(
                (line for line in s.splitlines() if line.lstrip().startswith('#')),
                '',
            ).strip()
            violations.append((node.lineno, offending[:120]))
    return violations


def _scanned_files() -> list[Path]:
    excluded_exact = {
        '__pycache__', '.git', 'venv', 'node_modules',
        'build', 'dist', '.pytest_cache', '.mypy_cache', 'frontend',
        'site-packages',
    }
    out: list[Path] = []
    for path in REPO_ROOT.rglob('*.py'):
        parts = path.parts
        if any(p in excluded_exact for p in parts):
            continue
        if any(p.startswith('.venv') or p.endswith('.egg-info') for p in parts):
            continue
        out.append(path)
    return out


class TestNoNosemgrepInsideSql:
    """The `# nosemgrep` directive must not land inside a SQL string."""

    def test_no_hash_comment_inside_sql_strings(self):
        all_violations: list[str] = []
        for py_file in _scanned_files():
            for lineno, snippet in _find_violations(py_file):
                rel = py_file.relative_to(REPO_ROOT)
                all_violations.append(f'{rel}:{lineno}: contains line {snippet!r}')

        assert not all_violations, (
            f'SQL strings with embedded `#`-prefix lines '
            f'({len(all_violations)} violation(s)). SQLite parses `#` as an '
            f'invalid token and the query fails at runtime. Move the '
            f'`# nosemgrep` directive to the LINE BEFORE the call.\n\n'
            f'Violations:\n' + '\n'.join(f'  {v}' for v in all_violations)
        )

    def test_broken_pattern_is_flagged(self):
        """Sanity: the antipattern from the bug IS detected."""
        stub = (
            'from sqlalchemy import text\n'
            'def f():\n'
            '    text(f"""  # nosemgrep: rule -- reason\n'
            '        SELECT 1\n'
            '    """)\n'
        )
        tmp = REPO_ROOT / 'tests' / 'test_contracts' / '_tc_nosemgrep_fixture.py'
        try:
            tmp.write_text(stub, encoding='utf-8')
            violations = _find_violations(tmp)
            assert len(violations) == 1, (
                f'Antipattern not flagged. Got: {violations}'
            )
        finally:
            tmp.unlink(missing_ok=True)

    def test_midline_antipattern_is_flagged(self):
        """Sanity: the mid-line variant (Gemini's catch — the f-string
        opens with SQL content on the same line as the call, with the
        directive trailing) IS detected."""
        stub = (
            'from sqlalchemy import text\n'
            'def f():\n'
            '    text(f"""SELECT id FROM users  # nosemgrep: rule -- reason\n'
            '             WHERE active = 1""")\n'
        )
        tmp = REPO_ROOT / 'tests' / 'test_contracts' / '_tc_nosemgrep_midline_fixture.py'
        try:
            tmp.write_text(stub, encoding='utf-8')
            violations = _find_violations(tmp)
            assert len(violations) == 1, (
                f'Mid-line antipattern not flagged. Got: {violations}'
            )
        finally:
            tmp.unlink(missing_ok=True)

    def test_clean_call_is_not_flagged(self):
        """Sanity: a properly-placed directive (line above) is fine."""
        stub = (
            'from sqlalchemy import text\n'
            'def f():\n'
            '    # nosemgrep: rule -- reason\n'
            '    text(f"""\n'
            '        SELECT 1\n'
            '    """)\n'
        )
        tmp = REPO_ROOT / 'tests' / 'test_contracts' / '_tc_nosemgrep_clean_fixture.py'
        try:
            tmp.write_text(stub, encoding='utf-8')
            assert _find_violations(tmp) == [], 'Clean call was flagged'
        finally:
            tmp.unlink(missing_ok=True)

    def test_dynamic_string_arg_is_not_flagged(self):
        """Variables, f-string interpolations, and other non-literal
        string args are out of scope — this contract only catches the
        easy regression vector where a literal string contains the
        directive."""
        stub = (
            'from sqlalchemy import text\n'
            'def f(query):\n'
            '    text(query)\n'
        )
        tmp = REPO_ROOT / 'tests' / 'test_contracts' / '_tc_nosemgrep_dyn_fixture.py'
        try:
            tmp.write_text(stub, encoding='utf-8')
            assert _find_violations(tmp) == [], 'Dynamic string was flagged'
        finally:
            tmp.unlink(missing_ok=True)
