"""Follow the small caller-owned deletion chain to its actual commit boundaries."""
import ast
from pathlib import Path


def deletion_is_atomic(root):
    helper_path = 'core/controlled_deletion.py'
    expected = {
        'redact_job_history_ids': {
            (helper_path, 'redact_deleted_job_history'),
            ('modules/jobs/bulk_deletion.py', 'delete_bulk_jobs'),
            ('modules/jobs/services.py', 'delete_org_jobs'),
            ('modules/system/routes_config.py', 'run_retention_cleanup')},
        'redact_deleted_job_history': {('modules/jobs/routes/jobs_crud.py', 'delete_job')},
        'delete_bulk_jobs': {('modules/jobs/routes/jobs_crud.py', 'bulk_update_jobs')},
        'delete_org_jobs': {('modules/organizations/routes.py', 'delete_org'),
                            ('modules/organizations/routes_users.py', 'delete_group')},
    }
    # Restrict the review to these exact callers, rejecting new unreviewed paths.
    found = {name: set() for name in expected}
    trees = {}
    for path in Path(root).rglob('*.py'):
        if 'schema' in path.parts and any(p.startswith('preserved') for p in path.parts):
            continue
        source = path.read_text()
        if not any(name in source for name in expected):
            continue
        relative = str(path.relative_to(root))
        tree = trees[relative] = ast.parse(source)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(fn):
                if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name) or call.func.id not in expected:
                    continue
                found[call.func.id].add((relative, fn.name))
                if not call.args or not isinstance(call.args[0], ast.Name) or call.args[0].id != 'db':
                    return False
                intermediates = {'redact_deleted_job_history', 'delete_bulk_jobs', 'delete_org_jobs'}
                commits = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                           and isinstance(n.func, ast.Attribute) and n.func.attr == 'commit'
                           and isinstance(n.func.value, ast.Name) and n.func.value.id == 'db']
                if fn.name in intermediates:
                    if commits:
                        return False
                elif not any(n.lineno > call.lineno for n in commits):
                    return False
    if found != expected:
        return False
    helper = next(n for n in trees[helper_path].body if isinstance(n, ast.FunctionDef) and n.name == 'redact_job_history_ids')
    if any(isinstance(n, ast.Name) and n.id == 'db' and isinstance(n.ctx, ast.Store) for n in ast.walk(helper)):
        return False
    for call in (n for n in ast.walk(helper) if isinstance(n, ast.Call)):
        if isinstance(call.func, ast.Attribute) and call.func.attr in ('commit', 'rollback'):
            return False
        if isinstance(call.func, ast.Name) and call.func.id == 'log_audit':
            if not isinstance(call.args[0], ast.Name) or call.args[0].id != 'db':
                return False
    return True
