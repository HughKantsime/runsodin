"""Shared SQL write-shape inspection for runtime guards."""
import re

COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
IDENT = r"[\"`\[]?\w+[\"`\]]?"
WRITE = re.compile(
    r"\b(INSERT\s+(?:OR\s+\w+\s+)?INTO|REPLACE\s+INTO|UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM|MERGE\s+INTO)\s+"
    rf"((?:{IDENT}\.)?{IDENT})", re.I)
SET_STOP = re.compile(r"(WHERE|RETURNING|FROM)\b", re.I)

def table_name(raw: str) -> str:
    return raw.split(".")[-1].strip('"`[]').lower()


def _top_level_set_clause(sql: str, start: int) -> str | None:
    """The SET clause after ``start``, cut at the first top-level WHERE/RETURNING/FROM/;.
    Parentheses and quotes are tracked so subqueries inside SET don't end it early."""
    m = re.search(r"\bSET\b", sql[start:], re.I)
    if not m:
        return None
    i = start + m.end()
    depth, quote, out = 0, None, []
    while i < len(sql):
        ch = sql[i]
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and (ch == ";" or (sql[i - 1:i].isspace() and SET_STOP.match(sql, i))):
            break
        out.append(ch)
        i += 1
    return "".join(out)


def _split_top_level(clause: str) -> list[str]:
    parts, depth, quote, cur = [], 0, None, []
    for ch in clause:
        if quote:
            quote = None if ch == quote else quote
        elif ch in "'\"`":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(cur)); cur = []
            continue
        cur.append(ch)
    parts.append("".join(cur))
    return parts


def set_columns(sql: str, start: int) -> frozenset[str]:
    """Columns an UPDATE assigns. Self-assignments (``SET id = id``, the row-lock idiom)
    change nothing and are left out; an empty result means a no-op lock."""
    clause = _top_level_set_clause(sql, start)
    if clause is None:
        return frozenset({"*"})
    tup = re.match(r"\s*\(([^)]*)\)\s*=", clause)
    if tup:  # tuple SET (a, b) = (...)
        return frozenset(n.strip().strip('"`[]').lower() for n in tup.group(1).split(","))
    columns = set()
    for part in _split_top_level(clause):
        lhs, eq, rhs = part.partition("=")
        if not eq:
            continue
        name = lhs.strip().split(".")[-1].strip('"`[]').lower()
        if rhs.strip().split(".")[-1].strip('"`[]').lower() == name:
            continue  # x = x
        columns.add(name)
    return frozenset(columns)


