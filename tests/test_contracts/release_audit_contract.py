"""Closed caller-owned audit protocols for the merged D3, D4 and E4 features."""
import ast

ORG = 'modules/organizations/'
PROTOCOLS = {
    ('modules/archives/filament_settlement.py', 'settle'): {
        'modules/archives/routes/filament_settlement.py': ('settle', {'settle_filament'}),
    },
    ('modules/printers/plate_clear.py', 'record_plate_clear'): {
        'modules/printers/routes_controls.py': ('record_plate_clear', {'plate_cleared'}),
    },
    ('modules/jobs/print_failure.py', 'change'): {
        'modules/jobs/routes/print_failure.py': ('change', {'_apply'}),
    },
    # _undo returns through change with the same session; the closed forwarding
    # edge is checked below before checking _apply's transaction boundary.
    ('modules/jobs/print_failure.py', '_undo'): {
        'modules/jobs/routes/print_failure.py': ('change', {'_apply'}),
    },
    (ORG + 'education_term_retention.py', 'audit'): {
        ORG + 'education_term_retention.py': ('audit', {'create_policy', 'delete_policy'}),
        ORG + 'education_term_cleanup.py': ('audit', {'record_blocked', 'finish_media', 'purge'}),
        ORG + 'education_term_privacy.py': ('audit', {'erase_student', 'resume_erasure', 'anonymize'}),
    },
    ('core/traceability_delivery.py', 'deliver_order'): {
        'modules/orders/routes/orders_crud.py': ('deliver_order', {'update_order', 'ship_order'}),
        'modules/orders/show_fulfillment.py': ('deliver_order', {'dispatch'}),
    },
    ('core/traceability_privacy.py', 'erase_personal'): {
        ORG + 'gdpr_erasure.py': ('erase_personal', {'erase_user_data_sync'}),
        ORG + 'routes_users.py': ('erase_personal', {'delete_user'}),
    },
    (ORG + 'education_program_audit.py', 'audit'): {
        ORG + 'education_program_records.py': ('audit', {'mark', 'approve_hours'}),
        ORG + 'routes_education_program_reports.py': ('audit', {'report'}),
    },
    (ORG + 'education_roster_store.py', 'audit'): {
        ORG + 'education_roster_store.py': ('audit', {'save'}),
        ORG + 'education_roster_sync.py': ('store.audit', {'preview'}),
        ORG + 'routes_education_roster.py': ('store.audit', {'clear_tombstone', 'import_roster'}),
        ORG + 'routes_roster_auth.py': ('store.audit', {'wrapped', 'begin', 'finish', 'callback', 'launch'}),
    },
}


def _calls(node, name):
    return [n for n in ast.walk(node) if isinstance(n, ast.Call) and ast.unparse(n.func) == name]


def caller_owned_audit_is_atomic(key, sources):
    """Require the shared session, no helper commit, and a later caller commit.

    This extends the existing lexical commit gate across only the listed call
    boundaries. Each invocation must reach a commit in its enclosing statement
    blocks, never merely in an unrelated conditional branch.
    """
    if key == ('modules/jobs/print_failure.py', '_undo'):
        tree = ast.parse(sources[key[0]])
        change = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'change')
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        calls = _calls(tree, '_undo')
        if (len(calls) != 1 or calls[0] not in list(ast.walk(change))
                or not isinstance(parents[calls[0]], ast.Return)
                or not calls[0].args or ast.unparse(calls[0].args[0]) != 'db'
                or _calls(change, 'db.commit') or _calls(change, 'db.rollback')):
            return False
    helper = next(n for n in ast.parse(sources[key[0]]).body
                  if isinstance(n, ast.FunctionDef) and n.name == key[1])
    if any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
           and n.func.attr in {'commit', 'rollback'} for n in ast.walk(helper)):
        return False
    audits = _calls(helper, 'log_audit')
    if len(audits) != 1 or not audits[0].args or ast.unparse(audits[0].args[0]) != 'db':
        return False
    for path, (name, expected) in PROTOCOLS[key].items():
        tree = ast.parse(sources[path])
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        def unsafe_rollback(node):
            for rollback in _calls(node, 'db.rollback'):
                ancestor = parents.get(rollback)
                while ancestor is not None and not isinstance(ancestor, ast.ExceptHandler):
                    ancestor = parents.get(ancestor)
                if ancestor is None or not isinstance(ancestor.body[-1], ast.Raise):
                    return True
            return False
        actual = set()
        for call in _calls(tree, name):
            if not call.args or ast.unparse(call.args[0]) != 'db':
                return False
            node = call
            committed = False
            while node in parents:
                parent = parents[node]
                if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module,
                                       ast.If, ast.Try, ast.ExceptHandler, ast.With, ast.For)):
                    for field in ('body', 'orelse', 'finalbody'):
                        block = getattr(parent, field, [])
                        if node not in block:
                            continue
                        for following in block[block.index(node) + 1:]:
                            if isinstance(following, ast.Expr) and isinstance(following.value, ast.Call) and ast.unparse(following.value.func) == 'db.commit':
                                committed = True
                                break
                            if isinstance(following, (ast.Return, ast.Raise, ast.Break, ast.Continue)) or unsafe_rollback(following):
                                if not committed:
                                    return False
                                break
                node = parent
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    actual.add(node.name)
                    break
            if not committed:
                return False
        if actual != expected:
            return False
    return True
