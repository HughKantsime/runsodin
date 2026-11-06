"""Recognize D2's audit helper only when every caller retains its commit boundary."""
import ast

FILES = ('education_classes_service.py', 'education_assignment_submissions.py',
         'education_assessments.py', 'education_review_service.py')
CALLERS = {
    ('education_classes_service.py', 'create_class'),
    ('education_classes_service.py', 'update_class'),
    ('education_classes_service.py', 'save_assignment'),
    ('education_assignment_submissions.py', 'link_submission'),
    ('education_assignment_submissions.py', '_try_printer'),
    ('education_assessments.py', 'assess'),
}


def classes_audit_is_atomic(sources):
    functions = {(name, node.name): node for name, source in sources.items()
                 for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)}

    def calls(node, name):
        return [n for n in ast.walk(node) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name) and n.func.id == name]

    def db_commits(node):
        return [n for n in ast.walk(node) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute) and n.func.attr == 'commit'
                and isinstance(n.func.value, ast.Name) and n.func.value.id == 'db']

    helper = functions[('education_classes_service.py', 'audit')]
    audits = calls(helper, 'log_audit')
    if len(audits) != 1 or db_commits(helper):
        return False
    if not audits[0].args or ast.unparse(audits[0].args[0]) != 'db':
        return False
    actual = {key for key, fn in functions.items() if calls(fn, 'audit')}
    if actual != CALLERS:
        return False
    for key in CALLERS:
        fn = functions[key]
        entries = calls(fn, 'audit')
        if len(entries) != 1 or ast.unparse(entries[0].args[0]) != 'db':
            return False
        audit = entries[0]
        commits = db_commits(fn)
        if key[1] == '_try_printer':
            # Review commits the automatic school audit and approval together.
            commits = calls(fn, 'approve_submission')
            review = functions[('education_review_service.py', 'approve_submission')]
            decisions = calls(review, '_record_decision')
            if not decisions or not any(c.lineno > decisions[-1].lineno for c in db_commits(review)):
                return False
            if len(commits) != 1 or ast.unparse(commits[0].args[0]) != 'db':
                return False
        if not any(c.lineno > audit.lineno for c in commits):
            return False
    return True
