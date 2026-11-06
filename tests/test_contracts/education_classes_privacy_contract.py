"""Closed D2 privacy inventory; writes now use static SQL without census exceptions."""
import ast


def privacy_writes_exclude_jobs(source):
    functions = {n.name: n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)}
    erase = functions['_erase_classes']
    loops = [n for n in ast.walk(erase) if isinstance(n, ast.For)]
    expected = {
        'table, key, fields': (
            ('education_classes', 'id', ('title', 'term')),
            ('education_assignments', 'id', ('title', 'brief')),
            ('education_assignment_submissions', 'submission_id', ('feedback', 'grade')),
            ('education_assessment_revisions', 'id', ('old_feedback', 'new_feedback', 'old_grade', 'new_grade'))),
    }
    if len(loops) != len(expected):
        return False
    for loop in loops:
        target = ast.unparse(loop.target).strip('()')
        try:
            if ast.literal_eval(loop.iter) != expected.get(target):
                return False
        except (ValueError, TypeError):
            return False
    updates = next(n.value for n in ast.parse(source).body if isinstance(n, ast.Assign)
                   and any(getattr(t, 'id', None) == '_SCRUB_UPDATES' for t in n.targets))
    statements = ast.literal_eval(updates)
    inventory = expected['table, key, fields']
    if set(statements) != {table for table, _, _ in inventory}:
        return False
    for table, key, fields in inventory:
        suffix = '' if table == 'education_assessment_revisions' else ',revision=revision+1'
        if statements[table] != ('UPDATE ' + table + ' SET ' +
                ','.join(field + '=:' + field for field in fields) + suffix + ' WHERE ' + key + '=:key'):
            return False
    callers = [(fn.name, n) for fn in functions.values() for n in ast.walk(fn)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == '_scrub_rows']
    return (len(callers) == 1 and callers[0][0] == '_erase_classes'
            and [ast.unparse(a) for a in callers[0][1].args] == ['db', 'table', 'key', 'fields', 'identifiers'])
