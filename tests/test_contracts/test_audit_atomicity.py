"""
Contract test — log_audit must share the mutation transaction.

Guards R7 from the 2026-04-12 Codex adversarial review:
    log_audit() used to open its own commit boundary. Callers committed
    business changes first, then called log_audit() which internally did
    ANOTHER commit. If the audit insert failed (lock contention, constraint
    violation), the client saw a 500 and retried — but the original side
    effect had already persisted. Wrong error model; produced duplicate
    operations whose origin was invisible because the audit trail was
    exactly the part that failed.

This test is a source-level gate that guarantees:
  1. log_audit() does NOT call db.commit() internally
  2. Every caller of log_audit() has a db.commit() in the same handler
     (or calls another function that does — we can't inspect that perfectly
     statically, so we rely on a stronger check: the commit must appear
     lexically after log_audit within the enclosing function)

Run without container: pytest tests/test_contracts/test_audit_atomicity.py -v
"""

import ast

import pytest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
DEPENDENCIES = BACKEND_DIR / "core" / "dependencies.py"
AUDIT_IMPL = BACKEND_DIR / "core" / "audit.py"


PRESERVED_BUNDLES = (BACKEND_DIR / "core/schema/preserved027/bundle", BACKEND_DIR / "core/schema/preserved029/bundle", BACKEND_DIR / "core/schema/preserved034/bundle")


def _enumerate_log_audit_call_sites() -> list[tuple[Path, int, str]]:
    """Return [(file, line_no, function_name)] for every log_audit() call in backend/."""
    sites = []
    for py in BACKEND_DIR.rglob("*.py"):
        # Skip the definition itself
        if py == DEPENDENCIES:
            continue
        # Hash-pinned frozen sources used only to stage historical restores; not live code.
        if any(bundle in py.parents for bundle in PRESERVED_BUNDLES):
            continue
        try:
            source = py.read_text()
        except Exception:
            continue
        if "log_audit(" not in source:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        # Walk: find every Call whose .func.id == 'log_audit', and record enclosing function
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Call):
                        fn = inner.func
                        name = getattr(fn, "id", None) or getattr(fn, "attr", None)
                        if name == "log_audit":
                            sites.append((py, inner.lineno, node.name))
    return sites


def _revision_audit_transaction_is_atomic(source: str) -> bool:
    """Recognize only the explicit model-revision transaction protocol."""
    tree = ast.parse(source)
    functions = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    helper = functions.get("_insert_revision")
    manager = functions.get("_revision_transaction")
    if helper is None or manager is None:
        return False

    def named_call(node, name):
        return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name

    def db_call(node, name):
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == name and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "db")

    if any(db_call(n, "commit") for n in ast.walk(helper)):
        return False
    audits = [n for n in ast.walk(helper) if named_call(n, "log_audit")]
    if len(audits) != 1 or not audits[0].args or ast.dump(audits[0].args[0]) != ast.dump(ast.Name(id="db", ctx=ast.Load())):
        return False
    if not any(isinstance(n, ast.Name) and n.id == "contextmanager" for n in manager.decorator_list):
        return False
    tries = [n for n in manager.body if isinstance(n, ast.Try)]
    if len(tries) != 1:
        return False
    transaction = tries[0]
    body = transaction.body
    # Permit only the reviewed straight-line protocol, not early exits or branches.
    if any(not isinstance(n, (ast.Expr, ast.Assign)) for n in body):
        return False
    if any(isinstance(n, (ast.Return, ast.Break, ast.Continue, ast.Raise)) for statement in body for n in ast.walk(statement)):
        return False
    # These operations must occur on the unconditional normal path, in order.
    yields = [i for i, n in enumerate(body) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Yield)]
    flushes = [i for i, n in enumerate(body) if isinstance(n, ast.Expr) and db_call(n.value, "flush")]
    commits = [i for i, n in enumerate(body) if isinstance(n, ast.Expr) and db_call(n.value, "commit")]
    if len(yields) != 1 or len(flushes) != 1 or len(commits) != 1 or not yields[0] < flushes[0] < commits[0]:
        return False
    if sum(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(manager)) != 1:
        return False
    if sum(db_call(n, "commit") for n in ast.walk(manager)) != 1:
        return False
    if len(transaction.handlers) != 1 or transaction.orelse or transaction.finalbody:
        return False
    handler = transaction.handlers[0]
    if not isinstance(handler.type, ast.Name) or handler.type.id != "Exception":
        return False
    # Rollback can be guarded against its own failure, but not conditionally skipped.
    rollback_attempt = any(isinstance(n, ast.Expr) and db_call(n.value, "rollback") for n in handler.body)
    for n in handler.body:
        if isinstance(n, ast.Try) and len(n.body) == 1 and isinstance(n.body[0], ast.Expr) and db_call(n.body[0].value, "rollback"):
            rollback_attempt = True
    if not rollback_attempt:
        return False
    # A final raise prevents a caught failure from looking like success.
    if not handler.body or not isinstance(handler.body[-1], ast.Raise):
        return False
    if any(isinstance(n, ast.Return) for n in ast.walk(handler)):
        return False

    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    calls = [n for n in ast.walk(tree) if named_call(n, "_insert_revision")]
    if not calls:
        return False
    for call in calls:
        if not call.args or not isinstance(call.args[0], ast.Name) or call.args[0].id != "db":
            return False
        ancestor = parents.get(call)
        wrapper = None
        while ancestor is not None and not isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if isinstance(ancestor, ast.With):
                for item in ancestor.items:
                    context = item.context_expr
                    if named_call(context, "_revision_transaction") and context.args and isinstance(context.args[0], ast.Name) and context.args[0].id == "db":
                        wrapper = ancestor
            ancestor = parents.get(ancestor)
        if wrapper is None or any(db_call(n, "commit") for statement in wrapper.body for n in ast.walk(statement)):
            return False
    return True


def _currency_audit_transaction_is_atomic(helper_source: str, caller_source: str) -> bool:
    """Recognize the narrow audited helper and its actual caller transaction."""
    helper_tree, caller_tree = ast.parse(helper_source), ast.parse(caller_source)
    helpers = [n for n in helper_tree.body if isinstance(n, ast.FunctionDef) and n.name == "write_currency"]
    if len(helpers) != 1:
        return False
    helper = helpers[0]

    def call(n, name, receiver=None):
        if not isinstance(n, ast.Call):
            return False
        if receiver is None:
            return isinstance(n.func, ast.Name) and n.func.id == name
        return (isinstance(n.func, ast.Attribute) and n.func.attr == name
                and isinstance(n.func.value, ast.Name) and n.func.value.id == receiver)

    if any(call(n, "commit", "db") or call(n, "rollback", "db") for n in ast.walk(helper)):
        return False
    audits = [n for n in ast.walk(helper) if call(n, "log_audit")]
    if len(audits) != 1 or not audits[0].args or not isinstance(audits[0].args[0], ast.Name) or audits[0].args[0].id != "db":
        return False
    parents = {c: n for n in ast.walk(caller_tree) for c in ast.iter_child_nodes(n)}
    calls = [n for n in ast.walk(caller_tree) if call(n, "write_currency")]
    if not calls:
        return False
    for invocation in calls:
        if not invocation.args or not isinstance(invocation.args[0], ast.Name) or invocation.args[0].id != "db":
            return False
        statement = parents.get(invocation)
        # Only the reviewed straight-line assignment is admitted, never a nested call.
        if not isinstance(statement, ast.Assign) or statement.value is not invocation:
            return False
        transaction = parents.get(statement)
        if not isinstance(transaction, ast.Try) or transaction.orelse or transaction.finalbody:
            return False
        if len(transaction.body) != 2 or transaction.body[0] is not statement:
            return False
        commit = transaction.body[1]
        if not isinstance(commit, ast.Expr) or not call(commit.value, "commit", "db"):
            return False
        if len(transaction.handlers) != 1:
            return False
        handler = transaction.handlers[0]
        if not isinstance(handler.type, ast.Name) or handler.type.id != "BaseException":
            return False
        if len(handler.body) != 2 or not isinstance(handler.body[0], ast.Expr) or not call(handler.body[0].value, "rollback", "db") or not isinstance(handler.body[1], ast.Raise) or handler.body[1].exc is not None:
            return False
    return True


def _retirement_audit_transaction_is_atomic(source: str) -> bool:
    """Recognize the reviewed helper/caller transaction, not arbitrary exemptions."""
    tree = ast.parse(source)
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    helper, manager = functions.get('_complete'), functions.get('_transaction')
    if helper is None or manager is None:
        return False
    if tuple(arg.arg for arg in helper.args.args) != ('db','principal','plan'):
        return False
    if any(isinstance(n,ast.Name) and n.id=='db' and isinstance(n.ctx,ast.Store) for n in ast.walk(helper)):
        return False
    def named(n, name):
        return isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
    def db_call(n, name):
        return (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name) and n.func.value.id == 'db'
                and n.func.attr == name)
    def names(n):
        return tuple(e.id if isinstance(e, ast.Name) else None for e in n.elts) if isinstance(n, ast.Tuple) else ()
    def transaction_end(n):
        # This closed recognizer accepts no transaction-ending method in the
        # helper/caller, regardless of receiver spelling or alias assignment.
        return (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr in ('commit', 'rollback'))
    if any(transaction_end(n) for n in ast.walk(helper)):
        return False
    audit = [n for n in ast.walk(helper) if named(n, 'log_audit')]
    if (len(audit) != 1 or not audit[0].args or not isinstance(audit[0].args[0], ast.Name)
            or audit[0].args[0].id != 'db'
            or not any(isinstance(n, ast.Expr) and n.value is audit[0] for n in helper.body)):
        return False
    if not any(isinstance(n, ast.Name) and n.id == 'contextmanager' for n in manager.decorator_list):
        return False
    if any(isinstance(n, (ast.Return, ast.Break, ast.Continue)) for n in ast.walk(manager)):
        return False
    tries = [n for n in manager.body if isinstance(n, ast.Try)]
    if len(tries) != 1:
        return False
    transaction = tries[0]
    yields = [i for i,n in enumerate(transaction.body) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Yield)]
    commits = [i for i,n in enumerate(transaction.body) if isinstance(n, ast.Expr) and db_call(n.value, 'commit')]
    if len(yields) != 1 or len(commits) != 1 or yields[0] >= commits[0]:
        return False
    if names(transaction.body[yields[0]].value.value) != ('db','principal','actual'):
        return False
    if sum(isinstance(n, (ast.Yield,ast.YieldFrom)) for n in ast.walk(manager)) != 1:
        return False
    if sum(db_call(n, 'commit') for n in ast.walk(manager)) != 1:
        return False
    sessions = [n for n in ast.walk(manager) if named(n, 'Session')]
    if len(sessions) != 1 or sessions[0].args or len(sessions[0].keywords) != 1:
        return False
    bind = sessions[0].keywords[0]
    if bind.arg != 'bind' or not isinstance(bind.value, ast.Name) or bind.value.id != 'owned_connection':
        return False
    assignments = [n for n in transaction.body if isinstance(n, ast.Assign) and any(isinstance(t,ast.Name) and t.id=='db' for t in n.targets)]
    if len(assignments) != 1 or assignments[0].value is not sessions[0] or assignments[0].lineno >= transaction.body[yields[0]].lineno:
        return False
    connections = [n for n in transaction.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='owned_connection' for t in n.targets)]
    if len(connections)!=1 or transaction.body.index(connections[0])>=transaction.body.index(assignments[0]):
        return False
    connect=connections[0].value
    if (not isinstance(connect,ast.Call) or not isinstance(connect.func,ast.Attribute)
            or not isinstance(connect.func.value,ast.Name) or connect.func.value.id!='engine'
            or connect.func.attr!='connect' or connect.args or connect.keywords):
        return False
    if not transaction.handlers or any(not h.body or not isinstance(h.body[-1],ast.Raise) or h.body[-1].exc is not None for h in transaction.handlers):
        return False
    if any(isinstance(n, ast.Name) and n.id=='db' and isinstance(n.ctx,ast.Store)
           for stmt in transaction.body[yields[0]+1:] for n in ast.walk(stmt)):
        return False
    parents = {child:parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    calls = [n for n in ast.walk(tree) if named(n, '_complete')]
    if not calls:
        return False
    for call in calls:
        expr = parents.get(call)
        owner = parents.get(expr)
        if (not isinstance(expr,ast.Expr) or not isinstance(owner,ast.With)
                or call.keywords or len(call.args)!=3
                or tuple(n.id if isinstance(n,ast.Name) else None for n in call.args)!=('db','principal','actual')):
            return False
        if not any(named(item.context_expr,'_transaction') and names(item.optional_vars)==('db','principal','actual') for item in owner.items):
            return False
        if any(transaction_end(n) for stmt in owner.body for n in ast.walk(stmt)):
            return False
        if any(isinstance(n,ast.Name) and n.id in ('db','principal','actual') and isinstance(n.ctx,ast.Store) for stmt in owner.body for n in ast.walk(stmt)):
            return False
    return True


class TestLogAuditDoesNotCommitInternally:
    """log_audit itself must not commit — caller owns the transaction."""

    def test_log_audit_function_body_has_no_commit(self):
        source = AUDIT_IMPL.read_text()
        tree = ast.parse(source)
        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "log_audit":
                target = node
                break
        assert target is not None, "log_audit is missing from core.audit"

        # Walk the body, check no db.commit() call
        for inner in ast.walk(target):
            if isinstance(inner, ast.Call):
                fn = inner.func
                if isinstance(fn, ast.Attribute) and fn.attr == "commit":
                    if isinstance(fn.value, ast.Name) and fn.value.id == "db":
                        ast_src = ast.get_source_segment(source, inner) or ""
                        raise AssertionError(
                            f"log_audit() calls db.commit() internally. "
                            f"This breaks the R7 invariant: audit and business "
                            f"mutation must share a transaction. Remove the "
                            f"internal commit — the caller's commit covers both. "
                            f"Offending call: {ast_src!r}"
                        )

    def test_log_audit_docstring_tells_callers_to_commit(self):
        """The docstring is load-bearing — if someone removes it, future
        callers lose the invariant."""
        source = AUDIT_IMPL.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "log_audit":
                docstring = ast.get_docstring(node) or ""
                assert "commit" in docstring.lower(), (
                    "log_audit's docstring must tell callers they own the commit. "
                    "Without this guidance, a future refactor can re-introduce "
                    "the commit-inside-log_audit bug."
                )
                return
        raise AssertionError("log_audit function not found")


def _telemetry_audit_transaction_is_atomic(sources):
    """Telemetry stages departures with slot updates; each caller commits that session.

    Like the revision/currency exceptions, this verifies the actual protocol,
    rather than exempting an unaudited helper by name.
    """
    helper_path = "modules/inventory/slot_assignments.py"
    expected = {helper_path: 1, "modules/printers/routes_ams.py": 3}
    found = {}
    for path, source in sources.items():
        tree = ast.parse(source)
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        def db_call(node, name):
            return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "db"
                    and node.func.attr == name)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "observe_slots" and path == helper_path:
                if any(db_call(n, "commit") or db_call(n, "rollback") for n in ast.walk(node)):
                    return False
                audits = [n for n in ast.walk(node) if isinstance(n, ast.Call)
                          and isinstance(n.func, ast.Name) and n.func.id == "log_audit"]
                if len(audits) != 1 or ast.unparse(audits[0].args[0]) != "db":
                    return False
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "observe_slots"):
                continue
            found[path] = found.get(path, 0) + 1
            if not node.args or ast.unparse(node.args[0]) != "db":
                return False
            # Require an unconditional subsequent commit in this very statement
            # block. A conditional/nested commit or early exit is not evidence.
            statement = parents[node]
            block = parents.get(statement)
            body = getattr(block, "body", [])
            if statement not in body:
                return False
            tail = body[body.index(statement) + 1:]
            commit = next((i for i, n in enumerate(tail) if isinstance(n, ast.Expr)
                           and db_call(n.value, "commit")), None)
            if commit is None:
                return False
            def unsafe_rollback(node):
                if not db_call(node, "rollback"):
                    return False
                ancestor = parents.get(node)
                while ancestor is not None and not isinstance(ancestor, ast.ExceptHandler):
                    ancestor = parents.get(ancestor)
                return ancestor is None or not isinstance(ancestor.body[-1], ast.Raise)
            if any(isinstance(n, ast.Return) or unsafe_rollback(n) or db_call(n, "commit")
                   for stmt in tail[:commit] for n in ast.walk(stmt)):
                return False
            if any(isinstance(stmt, (ast.Break, ast.Continue)) for stmt in tail[:commit]):
                return False
    return found == expected


def _telemetry_sources():
    return {str(path.relative_to(BACKEND_DIR)): path.read_text()
            for path in BACKEND_DIR.rglob("*.py")
            if not any(bundle in path.parents for bundle in PRESERVED_BUNDLES)
            and "observe_slots" in path.read_text()}


class TestEveryCallSiteCommitsAfter:
    """Every log_audit() call must be followed by db.commit() in the same function."""

    def test_every_call_site_has_subsequent_commit(self):
        sites = _enumerate_log_audit_call_sites()
        assert len(sites) >= 40, (
            f"Found only {len(sites)} log_audit call sites in backend/. "
            f"Expected ~50+. Something is filtering calls out — check the "
            f"AST walk, or the codebase shrank unexpectedly."
        )

        missing = []
        for py, line_no, fn_name in sites:
            source = py.read_text()
            tree = ast.parse(source)

            # Find the containing function node
            fn_node = None
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name == fn_name and node.lineno <= line_no <= (node.end_lineno or node.lineno):
                        fn_node = node
                        break
            if fn_node is None:
                continue  # couldn't resolve — skip, will be caught by other checks

            # Look for a db.commit() call AFTER line_no inside this function
            has_commit = False
            for inner in ast.walk(fn_node):
                if isinstance(inner, ast.Call) and inner.lineno > line_no:
                    fn_call = inner.func
                    if isinstance(fn_call, ast.Attribute) and fn_call.attr == "commit":
                        if isinstance(fn_call.value, ast.Name) and fn_call.value.id == "db":
                            has_commit = True
                            break

            if (not has_commit and py == BACKEND_DIR / "modules/models_library/routes/models_crud.py"
                    and fn_name == "_insert_revision"):
                has_commit = _revision_audit_transaction_is_atomic(source)
            if (not has_commit and py == BACKEND_DIR / "core/finance_currency.py"
                    and fn_name == "write_currency"):
                caller_paths = []
                for candidate in BACKEND_DIR.rglob("*.py"):
                    if any(bundle in candidate.parents for bundle in PRESERVED_BUNDLES):
                        continue
                    candidate_source = candidate.read_text()
                    candidate_tree = ast.parse(candidate_source)
                    if any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                           and n.func.id == "write_currency" for n in ast.walk(candidate_tree)):
                        caller_paths.append(candidate)
                has_commit = bool(caller_paths) and all(
                    _currency_audit_transaction_is_atomic(source, caller.read_text())
                    for caller in caller_paths
                )
            if (not has_commit and py == BACKEND_DIR / "modules/archives/timelapse_lifetime_action.py"
                    and fn_name == "_complete"):
                has_commit = _retirement_audit_transaction_is_atomic(source)
            if (not has_commit and py == BACKEND_DIR / "modules/inventory/slot_assignments.py"
                    and fn_name == "observe_slots"):
                has_commit = _telemetry_audit_transaction_is_atomic(_telemetry_sources())
            if not has_commit:
                missing.append(f"{py.relative_to(BACKEND_DIR.parent)}:{line_no} in {fn_name}()")

        assert not missing, (
            "Found log_audit() call sites with no db.commit() afterwards in "
            "the same function. R7 requires audit staged + caller commits "
            "atomically. Offenders:\n"
            + "\n".join(f"  {m}" for m in missing)
        )


_REVISION_PROTOCOL = """
@contextmanager
def _revision_transaction(db, model_id):
    try:
        yield {}
        db.flush()
        db.commit()
    except Exception:
        db.rollback()
        raise

def _insert_revision(db, model_id):
    log_audit(db, "created", "model", model_id)

def create(db, model_id):
    with _revision_transaction(db, model_id):
        _insert_revision(db, model_id)
"""


def test_revision_audit_protocol_recognizes_caller_owned_transaction():
    assert _revision_audit_transaction_is_atomic(_REVISION_PROTOCOL)
    source = (BACKEND_DIR / "modules/models_library/routes/models_crud.py").read_text()
    assert _revision_audit_transaction_is_atomic(source)


@pytest.mark.parametrize("before, after", [
    ("with _revision_transaction(db, model_id):", "if True:"),
    ("with _revision_transaction(db, model_id):", "with _revision_transaction(other_db, model_id):"),
    ("_insert_revision(db, model_id)\n", "_insert_revision(other_db, model_id)\n"),
    ("yield {}\n        db.flush()\n        db.commit()", "db.commit()\n        yield {}\n        db.flush()"),
    ("        db.flush()\n", ""),
    ("        db.rollback()\n", ""),
    ("        db.commit()", "        if ready:\n            db.commit()"),
    ("        raise\n", "        pass\n"),
    ("        raise\n", "        return\n"),
    ("@contextmanager\n", ""),
    ("        yield {}\n", "        yield {}\n        return\n"),
    ("        db.rollback()\n", "        if False:\n            db.rollback()\n"),
    ("    except Exception:", "    except ValueError:"),
    ("    log_audit(db,", "    db.commit()\n    log_audit(db,"),
    ("    log_audit(db,", "    log_audit(other_db,"),
])
def test_revision_audit_protocol_rejects_broken_boundaries(before, after):
    assert before in _REVISION_PROTOCOL
    assert not _revision_audit_transaction_is_atomic(_REVISION_PROTOCOL.replace(before, after))


def test_revision_audit_protocol_rejects_new_unwrapped_caller():
    source = _REVISION_PROTOCOL + "\ndef another(db, model_id):\n    _insert_revision(db, model_id)\n"
    assert not _revision_audit_transaction_is_atomic(source)


def test_revision_audit_protocol_rejects_caller_commit_inside_wrapper():
    source = _REVISION_PROTOCOL.replace("        _insert_revision(db, model_id)", "        db.commit()\n        _insert_revision(db, model_id)")
    assert not _revision_audit_transaction_is_atomic(source)


def test_currency_audit_protocol_recognizes_real_caller_owned_transaction():
    assert _currency_audit_transaction_is_atomic(
        (BACKEND_DIR / "core/finance_currency.py").read_text(),
        (BACKEND_DIR / "modules/orders/routes/finance_settings.py").read_text(),
    )


@pytest.mark.parametrize("before,after", [
    ("currency = write_currency(db,", "currency = write_currency(other_db,"),
    ("        db.commit()", "        pass"),
    ("        db.commit()", "        if ready: db.commit()"),
    ("        db.rollback()", "        pass"),
    ("    except BaseException:", "    except ValueError:"),
    ("        raise\n", "        return None\n"),
])
def test_currency_audit_protocol_rejects_broken_caller_boundaries(before, after):
    caller = (BACKEND_DIR / "modules/orders/routes/finance_settings.py").read_text()
    assert before in caller
    assert not _currency_audit_transaction_is_atomic(
        (BACKEND_DIR / "core/finance_currency.py").read_text(), caller.replace(before, after)
    )


def test_currency_audit_protocol_rejects_helper_commit_and_unwrapped_new_caller():
    helper = (BACKEND_DIR / "core/finance_currency.py").read_text()
    caller = (BACKEND_DIR / "modules/orders/routes/finance_settings.py").read_text()
    assert not _currency_audit_transaction_is_atomic(helper.replace("    log_audit(db,", "    db.commit()\n    log_audit(db,"), caller)
    assert not _currency_audit_transaction_is_atomic(helper, caller + "\ndef unsafe(db):\n    write_currency(db, currency='USD')\n")


@pytest.mark.parametrize('mutation', ['wrong_session','early_commit','skipped_commit','swallowed_failure','wrong_helper_session','outside_transaction','wrong_engine','rebind_after_yield','yield_wrong_db','helper_rebind','caller_commit','caller_rollback','caller_alias_commit','helper_alias_commit','helper_alias_rollback'])
def test_retirement_transaction_recognizer_rejects_unsafe_boundaries(mutation):
    source=(BACKEND_DIR/'modules/archives/timelapse_lifetime_action.py').read_text()
    assert _retirement_audit_transaction_is_atomic(source)
    if mutation=='wrong_session':
        changed=source.replace('db=Session(bind=owned_connection)','db=Session(bind=engine)',1)
    elif mutation=='early_commit':
        changed=source.replace('        yield db,principal,actual','        db.commit()\n        yield db,principal,actual',1)
    elif mutation=='skipped_commit':
        changed=source.replace('        db.commit()','        if handoff is not None:db.commit()',1)
    elif mutation=='swallowed_failure':
        changed=source.replace('        raise\n    finally:', '        pass\n    finally:',1)
    elif mutation=='wrong_helper_session':
        changed=source.replace('_complete(db,principal,actual)','_complete(other_db,principal,actual)',1)
    elif mutation=='wrong_engine':
        changed=source.replace('owned_connection=engine.connect()','owned_connection=other_engine.connect()',1)
    elif mutation=='rebind_after_yield':
        changed=source.replace('        yield db,principal,actual','        yield db,principal,actual\n        db=other_db',1)
    elif mutation=='yield_wrong_db':
        changed=source.replace('        yield db,principal,actual','        yield other_db,principal,actual',1)
    elif mutation in ('caller_commit','caller_rollback','caller_alias_commit'):
        operation={'caller_commit':'db.commit()', 'caller_rollback':'db.rollback()',
                   'caller_alias_commit':'other=db; other.commit()'}[mutation]
        changed=source.replace('_complete(db,principal,actual)',operation+'; _complete(db,principal,actual)',1)
    elif mutation in ('helper_alias_commit','helper_alias_rollback'):
        operation='commit' if mutation=='helper_alias_commit' else 'rollback'
        changed=source.replace('def _complete(db,principal,plan):',
                               'def _complete(db,principal,plan):\n    other=db; other.'+operation+'()',1)
    elif mutation=='helper_rebind':
        changed=source.replace('def _complete(db,principal,plan):','def _complete(db,principal,plan):\n    db=other_db',1)
    else:
        changed=source+'\ndef unsafe_retirement(db,principal,actual):\n    _complete(db,principal,actual)\n'
    assert changed!=source
    assert not _retirement_audit_transaction_is_atomic(changed)


def test_telemetry_audit_protocol_recognizes_caller_owned_transaction():
    assert _telemetry_audit_transaction_is_atomic(_telemetry_sources())


@pytest.mark.parametrize("mutation", ["missing_commit", "wrong_session", "new_caller", "helper_commit"])
def test_telemetry_audit_protocol_rejects_broken_boundaries(mutation):
    sources = _telemetry_sources()
    path = "modules/inventory/slot_assignments.py"
    if mutation == "missing_commit":
        sources[path] = sources[path].replace("            db.commit()", "            db.flush()")
    elif mutation == "wrong_session":
        sources[path] = sources[path].replace("self.observe_slots(db,", "self.observe_slots(other,")
    elif mutation == "helper_commit":
        sources[path] = sources[path].replace("        from core.dependencies import log_audit", "        db.commit()\n        from core.dependencies import log_audit")
    else:
        sources["modules/unsafe.py"] = "def unsafe(db):\n    provider.observe_slots(db, 1, [], set(), {})\n    db.commit()\n"
    assert not _telemetry_audit_transaction_is_atomic(sources)
