"""Static census of jobs.status writers and job deleters, per call site.

Used by test_cert_D_job_state_machine (only the state machine writes jobs.status)
and test_cert_D_job_attempt_cleanup (every job deletion clears its print-attempt
markers). Python code is read with ``ast``; SQL is read from every string the
code builds (constants, f-strings, implicit and ``+`` concatenation).

Shapes covered:
  SQL      UPDATE jobs [AS] alias SET ... [alias.]status=...   (quoted names too)
           INSERT INTO jobs ... ON CONFLICT ... DO UPDATE SET ... status=...
           INSERT OR REPLACE INTO jobs / REPLACE INTO jobs
           UPDATE jobs SET <dynamic>  (an f-string/concatenated SET list)
           UPDATE <dynamic> SET ...   (an interpolated table name)
           DELETE FROM jobs [AS alias]
  ORM      <job>.status = / += ; setattr(<job>, 'status' | <expr>, ...)
           update(Job) / sa_update(Job) / Job.__table__.update() / insert(Job)...on_conflict_do_update
           query(Job)...update(...) / bulk_update_mappings(Job, ...)
           <session>.delete(<job>) / delete(Job) / sa_delete(Job) / Job.__table__.delete()
           query(Job)...delete()
  where <job> is any name bound to a Job: a parameter annotated Job (or named
  job / *_job / job_*), a variable assigned from query(Job)/get(Job, ...)/Job(...)/
  select(Job), or a loop variable over one -- never a print_job.
"""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
SKIP = ("preserved027", "__pycache__")

_Q = r"[\"'`\[]?"
_JOBS = rf"{_Q}jobs{_Q}(?![\w.])"
_ALIAS = r"(?:\s+(?:AS\s+)?(?!SET\b|WHERE\b)(\w+))?"
SQL_UPDATE = re.compile(rf"\bUPDATE\s+(?:ONLY\s+)?{_JOBS}{_ALIAS}\s+SET\b", re.I)
SQL_UPSERT = re.compile(rf"\bINSERT\s+INTO\s+{_JOBS}[\s\S]*?\bON\s+CONFLICT\b[\s\S]*?\bDO\s+UPDATE\s+SET\b", re.I)
SQL_REPLACE = re.compile(rf"\b(?:INSERT\s+OR\s+REPLACE|REPLACE)\s+INTO\s+{_JOBS}", re.I)
SQL_DELETE = re.compile(rf"\bDELETE\s+FROM\s+(?:ONLY\s+)?{_JOBS}", re.I)
DYNAMIC = "\x00"  # stands in for an interpolated or concatenated non-literal part


def _string_value(node):
    """The SQL text a string-building expression produces (non-literal parts -> DYNAMIC)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        # An interpolation keeps its source between markers, so {column}={column}
        # (a lock no-op) is told apart from {column}={value}.
        return "".join(part.value if isinstance(part, ast.Constant)
                       else f"{DYNAMIC}{ast.unparse(part.value)}{DYNAMIC}" for part in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _string_value(node.left), _string_value(node.right)
        if left is None and right is None:
            return None
        return (left if left is not None else DYNAMIC) + (right if right is not None else DYNAMIC)
    return None


def _set_clause(sql, start):
    return re.split(r"\bWHERE\b|\bRETURNING\b", sql[start:start + 1200], maxsplit=1, flags=re.I)[0]


def _sets_status(clause, alias=None):
    own = "jobs" if alias is None else rf"(?:jobs|{re.escape(alias)})"
    target = rf"(?<![\w.])(?:{own}\.)?{_Q}status{_Q}"
    # status=status (or =jobs.status / =<alias>.status) is a lock no-op, not a write;
    # status=excluded.status (an upsert) or any other value is a write.
    return re.search(rf"{target}\s*=\s*(?!(?:{own}\.)?{_Q}status{_Q}\s*(?:[,)]|$))", clause, re.I)


def sql_status_writes(sql):
    kinds = []
    for match in SQL_UPDATE.finditer(sql):
        clause = _set_clause(sql, match.end())
        if _sets_status(clause, match.group(1)):
            kinds.append("sql update")
        elif clause.strip().startswith(DYNAMIC) or clause.strip() == "":
            kinds.append("sql update (dynamic SET)")
    for match in re.finditer(rf"\bUPDATE\s+{DYNAMIC}[^{DYNAMIC}]*{DYNAMIC}\s+SET\b", sql, re.I):
        clause = _set_clause(sql, match.end())
        assignments = [a.strip() for a in clause.split(",")]
        dynamic_writes = [a for a in assignments if a.startswith(DYNAMIC)
                          and not re.fullmatch(rf"({DYNAMIC}[^{DYNAMIC}]+{DYNAMIC})\s*=\s*\1", a)]
        if _sets_status(clause) or dynamic_writes:
            kinds.append("sql update (dynamic table)")  # cannot tell which table: allowlist explicitly
    for match in SQL_UPSERT.finditer(sql):
        if _sets_status(_set_clause(sql, match.end())):
            kinds.append("sql upsert")
    if SQL_REPLACE.search(sql):
        kinds.append("sql replace")
    return kinds


def sql_deletes(sql):
    return ["sql delete"] * len(SQL_DELETE.findall(sql))


def _src(node):
    return ast.unparse(node)


_JOB_SOURCE = re.compile(r"query\(\s*Job\b|\.get\(\s*Job\b|\bselect\(\s*Job\b|^Job\(|\bget_or_404\(\s*Job\b")


def _job_named(name):
    return bool(re.fullmatch(r"(?:\w+_)?job(?:_\w+)?|j", name)) and "print_job" not in name


def _annotated_job(annotation):
    return annotation is not None and re.search(r"\bJob\b", _src(annotation)) is not None


class _Scanner(ast.NodeVisitor):
    def __init__(self, path):
        self.path = path
        self.stack = ["<module>"]
        self.bound = [set()]
        self.writes, self.deletes, self.clearing = [], [], {}

    # scopes -------------------------------------------------------------
    def _function(self, node):
        names = set()
        args = node.args
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
            if _annotated_job(arg.annotation) or _job_named(arg.arg):
                names.add(arg.arg)
        for child in ast.walk(node):
            if isinstance(child, ast.Assign) and _JOB_SOURCE.search(_src(child.value)):
                names |= {t.id for t in child.targets if isinstance(t, ast.Name)}
            if isinstance(child, (ast.For, ast.comprehension)) and _JOB_SOURCE.search(_src(child.iter)):
                target = child.target
                names |= {target.id} if isinstance(target, ast.Name) else set()
            if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name) and _annotated_job(child.annotation):
                names.add(child.target.id)
        qualified = ".".join([*self.stack[1:], node.name])
        self.clearing[qualified] = any(
            isinstance(c, ast.Call) and _src(c.func).split(".")[-1] == "clear_job_attempts" for c in ast.walk(node))
        self.stack.append(node.name)
        self.bound.append(self.bound[-1] | names)
        self.generic_visit(node)
        self.bound.pop()
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _function

    def _site(self):
        return ".".join(self.stack[1:]) or "<module>"

    def _is_job(self, node):
        return isinstance(node, ast.Name) and (node.id in self.bound[-1] or _job_named(node.id))

    def _write(self, kind, node):
        self.writes.append((self.path, self._site(), kind, node.lineno))

    def _delete(self, kind, node):
        self.deletes.append((self.path, self._site(), kind, node.lineno))

    # ORM ----------------------------------------------------------------
    def _targets(self, targets, node):
        for target in targets:
            for leaf in ast.walk(target):
                if isinstance(leaf, ast.Attribute) and leaf.attr == "status" and self._is_job(leaf.value):
                    self._write("orm attribute", node)

    def visit_Assign(self, node):
        self._targets(node.targets, node)
        self.generic_visit(node)

    def visit_AugAssign(self, node):
        self._targets([node.target], node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        self._targets([node.target], node)
        self.generic_visit(node)

    def visit_Call(self, node):
        func = _src(node.func)
        text_ = _src(node)
        if func == "setattr" and len(node.args) >= 2 and self._is_job(node.args[0]):
            field = node.args[1]
            if not (isinstance(field, ast.Constant) and field.value != "status"):
                self._write("setattr", node)
        if re.fullmatch(r"(?:\w+\.)?(?:sa_)?update", func) and node.args and _src(node.args[0]) == "Job":
            self._write("update(Job)", node)
        if func in ("Job.__table__.update",):
            self._write("Job.__table__.update", node)
        if func.endswith(".on_conflict_do_update") and re.search(r"\binsert\(\s*Job\b|Job\.__table__\.insert", text_):
            self._write("upsert(Job)", node)
        if func.endswith(".update") and re.search(r"query\(\s*Job\s*\)", _src(node.func)):
            self._write("query(Job).update", node)
        if func.endswith("bulk_update_mappings") and node.args and _src(node.args[0]) == "Job":
            self._write("bulk_update_mappings(Job)", node)
        if re.fullmatch(r"\w+\.delete", func) and node.args and self._is_job(node.args[0]) and not func.startswith("query"):
            self._delete("session.delete(job)", node)
        if re.fullmatch(r"(?:\w+\.)?(?:sa_)?delete", func) and node.args and _src(node.args[0]) == "Job":
            self._delete("delete(Job)", node)
        if func == "Job.__table__.delete":
            self._delete("Job.__table__.delete", node)
        if func.endswith(".delete") and re.search(r"query\(\s*Job\s*\)", _src(node.func)):
            self._delete("query(Job).delete", node)
        self.generic_visit(node)

    # SQL ----------------------------------------------------------------
    def _strings(self, node):
        sql = _string_value(node)
        if sql is None:
            return False
        for kind in sql_status_writes(sql):
            self._write(kind, node)
        for kind in sql_deletes(sql):
            self._delete(kind, node)
        return True

    def visit_BinOp(self, node):
        if not self._strings(node):
            self.generic_visit(node)

    def visit_JoinedStr(self, node):
        self._strings(node)

    def visit_Constant(self, node):
        self._strings(node)


def scan(sources=None):
    """(status writes, deletes, clearing functions) over the backend or ``{path: source}``."""
    if sources is None:
        sources = {}
        for path in sorted(BACKEND.rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if not any(part in rel for part in SKIP):
                sources[rel] = path.read_text(encoding="utf-8")
    writes, deletes, clearing = [], [], {}
    for rel, source in sources.items():
        scanner = _Scanner(rel)
        scanner.visit(ast.parse(source))
        writes += scanner.writes
        deletes += scanner.deletes
        clearing.update({(rel, name): value for name, value in scanner.clearing.items()})
    return writes, deletes, clearing
