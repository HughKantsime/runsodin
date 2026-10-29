"""Static census of writers to chosen table columns (and of deleters), per call site.

A ``Target`` names a table, its ORM model, the columns whose writes are
counted, whether inserting rows counts as a write, and how instance variables
are usually named. Used by:

  test_cert_D_job_state_machine   JOBS          only the state machine writes jobs.status
  test_cert_D_job_attempt_cleanup JOBS deletes  every job deletion clears its attempt markers
  test_cert_D_filament_ledger     SPOOLS, SPOOL_USAGE  only the ledger consumes filament

Python is read with ``ast``; SQL is read from every string the code builds
(constants, f-strings, implicit and ``+`` concatenation).

Shapes covered, for <t> the target table (optionally schema-qualified and
quoted, e.g. public."spools") and <c> a target column:
  SQL      UPDATE <t> [AS] alias SET ... [alias.]<c>=...
           INSERT INTO <t> ... ON CONFLICT ... DO UPDATE SET ... <c>=...
           INSERT [OR REPLACE] INTO <t> (... <c> ...)   (any insert, for insert targets)
           REPLACE INTO <t>
           UPDATE <t> SET <dynamic>  (an f-string/concatenated SET list)
           UPDATE <dynamic> SET <c>= / <dynamic>=<other>; INSERT INTO <dynamic> (insert targets)
           DELETE FROM <t> [AS alias]
  ORM      <inst>.<c> = / += ; setattr(<inst>, '<c>' | <expr>, ...); Model(<c>=...)
           (insert targets: any Model(...), insert(Model), insert(Model.__table__),
           Model.__table__.insert(), bulk_insert_mappings(Model, ...))
           update(Model) / sa_update(Model) / Model.__table__.update() / <table var>.update()
           insert(Model)...on_conflict_do_update(...)
           query(Model)...update(...) across multi-line chains / bulk_update_mappings(Model, ...)
           <session>.delete(<inst>) / delete(Model) / sa_delete(Model) / Model.__table__.delete()
           query(Model)...delete() / <table var>.delete()
  SQL may also be built with "...{}".format(...) or "...%s" % (...); comments are
  ignored; row-value SET (a, <c>) = (...), MERGE INTO <t> ... UPDATE SET/INSERT and
  column-less INSERT INTO <t> VALUES are writes. The model may be imported under
  another name (from ... import Job as J) or qualified (models.Job).
  where <inst> is any name bound to the model: a parameter annotated with it (or
  named by the target's naming pattern), a variable assigned from
  query(Model)/get(Model, ...)/Model(...)/select(Model), or a loop variable over
  one; a <table var> is a name assigned from Model.__table__ or Table('<t>', ...).
"""
import ast
import re
from collections import namedtuple
from dataclasses import dataclass
from pathlib import Path

# One call site: ``detail`` is the SQL text the node builds (interpolations
# bracketed by DYNAMIC) or the unparsed ORM call/assignment, for checks at the node.
Site = namedtuple("Site", "path site kind line detail")

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
# Frozen byte copies of released restore-stage sources (run only in the isolated restore child).
SKIP = ("preserved027", "preserved029/bundle/", "preserved034/bundle/", "preserved039/overlay/", "preserved040/overlay/", "preserved043/overlay/", "preserved045/overlay/", "__pycache__")
DYNAMIC = "\x00"  # brackets an interpolated or concatenated non-literal part
_Q = r"[\"'`\[\]]?"


@dataclass(frozen=True)
class Target:
    table: str
    model: str
    columns: frozenset
    inserts: bool = False     # inserting a row is itself a write (spool_usage)
    creation: bool = False    # creating a row with a target column set counts (spools; not a job's initial status)
    any_owner: bool = False   # the column name is distinctive: a write to it on any object counts
    naming: str = r"$^"   # instance names that count as bound without other evidence
    exclude: str = r"$^"  # names that never count (e.g. print_job for jobs)

    def table_re(self):
        return rf"(?:{_Q}\w+{_Q}\.)?{_Q}{self.table}{_Q}(?![\w.])"


JOBS = Target("jobs", "Job", frozenset({"status"}),
              naming=r"(?:\w+_)?job(?:_\w+)?|j", exclude=r".*print_job.*")
SPOOLS = Target("spools", "Spool", frozenset({"remaining_weight_g"}), creation=True, any_owner=True,
                naming=r"(?:\w+_)?spool(?:_\w+)?")
SPOOL_USAGE = Target("spool_usage", "SpoolUsage", frozenset({"weight_used_g"}), inserts=True, any_owner=True,
                     naming=r"(?:\w+_)?usage(?:_\w+)?")


def _string_value(node):
    """The SQL text a string-building expression produces (non-literal parts bracketed by DYNAMIC)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        # An interpolation keeps its source between markers, so {column}={column}
        # (a lock no-op) is told apart from {column}={value}.
        return "".join(part.value if isinstance(part, ast.Constant)
                       else f"{DYNAMIC}{ast.unparse(part.value)}{DYNAMIC}" for part in node.values)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format"
            and isinstance(node.func.value, ast.Constant) and isinstance(node.func.value.value, str)):
        # "...{}...".format(x) / "{table}".format(table=t): each field becomes an interpolation
        arguments = [ast.unparse(a) for a in node.args]
        keywords = {k.arg: ast.unparse(k.value) for k in node.keywords if k.arg}
        position = iter(range(len(arguments)))

        def field(match):
            name = match.group(1).split("!")[0].split(":")[0]
            if name == "":
                index = next(position, None)
                source = arguments[index] if index is not None else "?"
            elif name.isdigit():
                source = arguments[int(name)] if int(name) < len(arguments) else "?"
            else:
                source = keywords.get(name, name)
            return f"{DYNAMIC}{source}{DYNAMIC}"
        return re.sub(r"\{([^{}]*)\}", field, node.func.value.value)
    if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod)
            and isinstance(node.left, ast.Constant) and isinstance(node.left.value, str)):
        # "... %s ..." % x / "%(name)s" % {...}: each conversion becomes an interpolation
        right = ast.unparse(node.right)
        return re.sub(r"%(?:\((\w+)\))?[sdrif]", lambda m: f"{DYNAMIC}{m.group(1) or right}{DYNAMIC}", node.left.value)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _string_value(node.left), _string_value(node.right)
        if left is None and right is None:
            return None
        def part(value, side):
            return value if value is not None else f"{DYNAMIC}{ast.unparse(side)}{DYNAMIC}"
        return part(left, node.left) + part(right, node.right)
    return None


def _set_clause(sql, start):
    return re.split(r"\bWHERE\b|\bRETURNING\b|\bFROM\b", sql[start:start + 1200], maxsplit=1, flags=re.I)[0]


def _sets_column(clause, target, alias=None):
    own = target.table if alias is None else rf"(?:{target.table}|{re.escape(alias)})"
    for column in sorted(target.columns):
        name = rf"(?:{own}\.)?{_Q}{column}{_Q}"
        # <c>=<c> (or =<table>.<c>) is a lock no-op; <c>=excluded.<c> or any other value writes.
        if re.search(rf"(?<![\w.]){name}\s*=\s*(?!{name}\s*(?:[,)]|$))", clause, re.I):
            return True
        # Row-value form: SET (a, <c>, b) = (...)
        if re.search(rf"\([^()]*(?<![\w.]){name}(?![\w])[^()]*\)\s*=", clause, re.I):
            return True
    return False


def _dynamic_assignments(clause):
    return [a for a in (a.strip() for a in clause.split(",")) if a.startswith(DYNAMIC)
            and not re.fullmatch(rf"({DYNAMIC}[^{DYNAMIC}]+{DYNAMIC})\s*=\s*\1", a)]


def _without_comments(sql):
    return re.sub(r"--[^\n]*", " ", re.sub(r"/\*.*?\*/", " ", sql, flags=re.S))


def sql_writes(sql, target=JOBS):
    """Write kinds a SQL text performs on the target's columns (or rows, for insert targets)."""
    sql = _without_comments(sql)
    table = target.table_re()
    alias = r"(?:\s+(?:AS\s+)?(?!SET\b|WHERE\b)(\w+))?"
    kinds = []
    for match in re.finditer(rf"\bUPDATE\s+(?:ONLY\s+)?{table}{alias}\s+SET\b", sql, re.I):
        clause = _set_clause(sql, match.end())
        if _sets_column(clause, target, match.group(1)):
            kinds.append("sql update")
        elif clause.strip().startswith(DYNAMIC) or clause.strip() == "":
            kinds.append("sql update (dynamic SET)")
    for match in re.finditer(rf"\bUPDATE\s+{DYNAMIC}[^{DYNAMIC}]*{DYNAMIC}(?:{DYNAMIC}[^{DYNAMIC}]*{DYNAMIC})*\s+SET\b",
                             sql, re.I):
        clause = _set_clause(sql, match.end())
        if _sets_column(clause, target) or _dynamic_assignments(clause):
            kinds.append("sql update (dynamic table)")  # cannot tell which table: allowlist explicitly
    for match in re.finditer(rf"\bINSERT\s+INTO\s+{table}[\s\S]*?\bON\s+CONFLICT\b[\s\S]*?\bDO\s+UPDATE\s+SET\b", sql, re.I):
        if _sets_column(_set_clause(sql, match.end()), target):
            kinds.append("sql upsert")
    if re.search(rf"\b(?:INSERT\s+OR\s+REPLACE|REPLACE)\s+INTO\s+{table}", sql, re.I):
        kinds.append("sql replace")
    for match in re.finditer(rf"\bMERGE\s+INTO\s+{table}", sql, re.I):
        body = sql[match.end():match.end() + 2000]
        updates = [m.end() for m in re.finditer(r"\bUPDATE\s+SET\b", body, re.I)]
        if any(_sets_column(_set_clause(body, end), target) for end in updates) or (
                (target.inserts or target.creation) and re.search(r"\bTHEN\s+INSERT\b", body, re.I)):
            kinds.append("sql merge")
    for match in re.finditer(rf"\bINSERT\s+(?:OR\s+(?:IGNORE|ABORT|FAIL|ROLLBACK)\s+)?INTO\s+{table}\s*(\([^)]*\))?",
                             sql, re.I):
        columns = match.group(1) or ""
        # No column list (INSERT INTO t VALUES / SELECT) writes every column.
        if target.inserts or (target.creation and (
                not columns or any(re.search(rf"\b{c}\b", columns) for c in target.columns) or DYNAMIC in columns)):
            kinds.append("sql insert")
    if (target.inserts or target.creation) and re.search(rf"\bINSERT\s+INTO\s+{DYNAMIC}", sql, re.I):
        kinds.append("sql insert (dynamic table)")
    return kinds


def sql_status_writes(sql):
    """jobs.status writes in one SQL text (the runtime hook's check)."""
    return sql_writes(sql, JOBS)


def sql_deletes(sql, target=JOBS):
    return ["sql delete"] * len(re.findall(rf"\bDELETE\s+FROM\s+(?:ONLY\s+)?{target.table_re()}", sql, re.I))


class _Scanner(ast.NodeVisitor):
    def __init__(self, path, target):
        self.path, self.t = path, target
        model = re.escape(target.model)
        self.model_source = re.compile(rf"query\(\s*{model}\b|\.get\(\s*{model}\b|\bselect\(\s*{model}\b|^{model}\(|"
                                       rf"\bget_or_404\(\s*{model}\b")
        self.stack = ["<module>"]
        self.bound = [set()]
        self.tables = [set()]
        self.writes, self.deletes, self.clearing = [], [], {}

    # names --------------------------------------------------------------
    def _named(self, name):
        return bool(re.fullmatch(self.t.naming, name)) and not re.fullmatch(self.t.exclude, name)

    def _annotated(self, annotation):
        return annotation is not None and re.search(rf"\b{re.escape(self.t.model)}\b", ast.unparse(annotation))

    def _finder(self, value):
        """A call whose function is named after the model (find_spool_by_rfid(...), get_job(...))."""
        if not isinstance(value, ast.Call):
            return False
        name = ast.unparse(value.func).split(".")[-1].lower()
        word = re.sub(r"(?<!^)(?=[A-Z])", "_", self.t.model).lower()
        return re.search(rf"(?:^|_){word}(?:_|$)", name) is not None and not re.fullmatch(self.t.exclude, name)

    def _table_object(self, value):
        source = ast.unparse(value)
        return (source == f"{self.t.model}.__table__"
                or re.fullmatch(rf"(?:\w+\.)?Table\(\s*['\"]{self.t.table}['\"][\s\S]*\)", source) is not None)

    def _collect(self, node):
        names, tables = set(), set()
        args = getattr(node, "args", None)
        if args is not None:
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                if self._annotated(arg.annotation) or self._named(arg.arg):
                    names.add(arg.arg)
        for child in ast.walk(node):
            if isinstance(child, ast.Assign):
                targets = {t.id for t in child.targets if isinstance(t, ast.Name)}
                if self.model_source.search(ast.unparse(child.value)) or self._finder(child.value):
                    names |= targets
                if self._table_object(child.value):
                    tables |= targets
            if isinstance(child, (ast.For, ast.comprehension)) and self.model_source.search(ast.unparse(child.iter)):
                if isinstance(child.target, ast.Name):
                    names.add(child.target.id)
            if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name) and self._annotated(child.annotation):
                names.add(child.target.id)
        return names, tables

    def visit_Module(self, node):
        names, tables = self._collect(ast.Module(body=[n for n in node.body if not isinstance(
            n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))], type_ignores=[]))
        self.bound[-1] |= names
        self.tables[-1] |= tables
        self.generic_visit(node)

    def _function(self, node):
        names, tables = self._collect(node)
        qualified = ".".join([*self.stack[1:], node.name])
        self.clearing[qualified] = any(
            isinstance(c, ast.Call) and ast.unparse(c.func).split(".")[-1] == "clear_job_attempts" for c in ast.walk(node))
        self.stack.append(node.name)
        self.bound.append(self.bound[-1] | names)
        self.tables.append(self.tables[-1] | tables)
        self.generic_visit(node)
        self.tables.pop()
        self.bound.pop()
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _function

    def _site(self):
        return ".".join(self.stack[1:]) or "<module>"

    def _is_instance(self, node):
        return isinstance(node, ast.Name) and (node.id in self.bound[-1] or self._named(node.id))

    def _detail(self, node):
        sql = _string_value(node)
        return sql if sql is not None else ast.unparse(node)

    def _write(self, kind, node):
        self.writes.append(Site(self.path, self._site(), kind, node.lineno, self._detail(node)))

    def _delete(self, kind, node):
        self.deletes.append(Site(self.path, self._site(), kind, node.lineno, self._detail(node)))

    # ORM ----------------------------------------------------------------
    def _targets(self, targets, node):
        for target in targets:
            for leaf in ast.walk(target):
                if isinstance(leaf, ast.Attribute) and leaf.attr in self.t.columns and (
                        self.t.any_owner or self._is_instance(leaf.value)):
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
        if (isinstance(node.func, ast.Attribute) and node.func.attr == "format"
                and isinstance(node.func.value, ast.Constant) and isinstance(node.func.value.value, str)):
            self._strings(node)
            for child in [*node.args, *(k.value for k in node.keywords)]:
                self.visit(child)
            return
        func = ast.unparse(node.func)
        model = self.t.model
        first = ast.unparse(node.args[0]) if node.args else ""
        chain = func  # the whole receiver chain, however many lines it spans
        on_query = re.search(rf"query\(\s*{re.escape(model)}\s*\)", chain) is not None
        on_table = (isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in self.tables[-1])
        if func == "setattr" and len(node.args) >= 2 and (self._is_instance(node.args[0]) or (
                self.t.any_owner and isinstance(node.args[1], ast.Constant) and node.args[1].value in self.t.columns)):
            field = node.args[1]
            if not (isinstance(field, ast.Constant) and field.value not in self.t.columns):
                self._write("setattr", node)
        if func == model or func.endswith("." + model):
            if self.t.inserts:
                self._write(f"{model}()", node)
            elif self.t.creation and any(k.arg in self.t.columns or k.arg is None for k in node.keywords):
                self._write(f"{model}(column=...)", node)
        if re.fullmatch(r"(?:\w+\.)?(?:sa_)?update", func) and first in (model, f"{model}.__table__"):
            self._write(f"update({model})", node)
        if func == f"{model}.__table__.update" or (on_table and node.func.attr == "update"):
            self._write("table.update()", node)
        if self.t.inserts and (
                (re.fullmatch(r"(?:\w+\.)?(?:sa_|pg_|sqlite_)?insert", func) and first in (model, f"{model}.__table__"))
                or func == f"{model}.__table__.insert" or (on_table and node.func.attr == "insert")
                or (func.endswith("bulk_insert_mappings") and first == model)):
            self._write(f"insert({model})", node)
        if func.endswith(".on_conflict_do_update") and re.search(
                rf"\binsert\(\s*{re.escape(model)}\b|{re.escape(model)}\.__table__\.insert", ast.unparse(node)):
            self._write(f"upsert({model})", node)
        if func.endswith(".update") and on_query:
            self._write(f"query({model}).update", node)
        if func.endswith("bulk_update_mappings") and first == model:
            self._write(f"bulk_update_mappings({model})", node)
        if re.fullmatch(r"\w+\.delete", func) and node.args and self._is_instance(node.args[0]):
            self._delete("session.delete(instance)", node)
        if re.fullmatch(r"(?:\w+\.)?(?:sa_)?delete", func) and first in (model, f"{model}.__table__"):
            self._delete(f"delete({model})", node)
        if func == f"{model}.__table__.delete" or (on_table and node.func.attr == "delete"):
            self._delete("table.delete()", node)
        if func.endswith(".delete") and on_query:
            self._delete(f"query({model}).delete", node)
        self.generic_visit(node)

    # SQL ----------------------------------------------------------------
    def _strings(self, node):
        sql = _string_value(node)
        if sql is None:
            return False
        for kind in sql_writes(sql, self.t):
            self._write(kind, node)
        for kind in sql_deletes(sql, self.t):
            self._delete(kind, node)
        return True

    def visit_BinOp(self, node):
        if not self._strings(node):
            self.generic_visit(node)
        elif isinstance(node.op, ast.Mod):
            self.visit(node.right)

    def visit_JoinedStr(self, node):
        self._strings(node)

    def visit_Constant(self, node):
        self._strings(node)


def backend_sources(skip_files=()):
    sources = {}
    for path in sorted(BACKEND.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if not any(part in rel for part in SKIP) and not rel.startswith(tuple(skip_files)):
            sources[rel] = path.read_text(encoding="utf-8")
    return sources


class _ModelAliases(ast.NodeTransformer):
    """Rewrite `from x import Job as J` uses (J) and qualified uses (models.Job) to the model's name."""

    def __init__(self, model, tree):
        self.model = model
        self.aliases = {alias.asname for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                        for alias in node.names if alias.name == model and alias.asname}

    def visit_Name(self, node):
        return ast.copy_location(ast.Name(id=self.model, ctx=node.ctx), node) if node.id in self.aliases else node

    def visit_Attribute(self, node):
        self.generic_visit(node)
        if node.attr == self.model and isinstance(node.value, (ast.Name, ast.Attribute)):
            return ast.copy_location(ast.Name(id=self.model, ctx=node.ctx), node)
        return node


def scan(sources=None, target=JOBS):
    """(writes, deletes, clearing functions) over the backend or ``{path: source}``."""
    if sources is None:
        sources = backend_sources()
    writes, deletes, clearing = [], [], {}
    for rel, source in sources.items():
        scanner = _Scanner(rel, target)
        tree = ast.parse(source)
        scanner.visit(ast.fix_missing_locations(_ModelAliases(target.model, tree).visit(tree)))
        writes += scanner.writes
        deletes += scanner.deletes
        clearing.update({(rel, name): value for name, value in scanner.clearing.items()})
    return writes, deletes, clearing


def resolve(path, expression):
    """Evaluate a dotted constant (e.g. ``schema.TABLE``) in a backend module's namespace."""
    import importlib
    if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", expression):
        raise ValueError(f"not a dotted constant: {expression!r}")
    module = importlib.import_module(path.removeprefix("backend/").removesuffix(".py").replace("/", "."))
    return eval(expression, vars(module))  # noqa: S307 - a dotted name from our own source


def call_arguments(path, callee, index):
    """Unparsed positional argument ``index`` of every call to ``callee`` in a backend file."""
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    return [ast.unparse(node.args[index]) for node in ast.walk(tree)
            if isinstance(node, ast.Call) and ast.unparse(node.func) == callee and len(node.args) > index]


def function_source(path, name):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    node = next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    return ast.unparse(node)


def counts(sites):
    """{(path, function, kind): number of call sites}."""
    found = {}
    for site in sites:
        found[site[:3]] = found.get(site[:3], 0) + 1
    return found


def dynamic_parts(detail):
    """The interpolated expressions of a site's SQL text, in order."""
    return re.findall(rf"{DYNAMIC}([^{DYNAMIC}]+){DYNAMIC}", detail)
