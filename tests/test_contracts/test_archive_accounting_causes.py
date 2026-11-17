"""Reason vocabulary census, plain explanations and visible archive aggregation."""
import ast
import re
from pathlib import Path
from datetime import datetime, timedelta
import pytest
from tests.test_contracts.test_archive_filament_settlement import settlement_case, archive_case, review_db, write  # noqa: F401
from modules.archives.accounting_causes import CAUSES, DEFAULT, REASONS, describe


def reason_literals(node):
    """Literal output values, excluding predicates and dictionary lookup keys."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return reason_literals(node.body) | reason_literals(node.orelse)
    if isinstance(node, ast.BoolOp):
        return set().union(*(reason_literals(value) for value in node.values))
    return set()


def collect_reasons(source):
    found = set()
    tree = ast.parse(source)
    returned = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Return)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'unresolved' and node.args:
            found |= reason_literals(node.args[0])
        if isinstance(node, ast.keyword) and node.arg in ('reason', 'accounting_reason'):
            found |= reason_literals(node.value)
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value in ('reason', 'accounting_reason'):
                    found |= reason_literals(value)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                names = target.elts if isinstance(target, (ast.Tuple, ast.List)) else [target]
                values = node.value.elts if isinstance(node.value, (ast.Tuple, ast.List)) else [node.value]
                for name, value in zip(names, values):
                    if isinstance(name, ast.Name) and name.id in ('reason', 'accounting_reason'):
                        found |= reason_literals(value)
        if id(node) in returned and isinstance(node, ast.Tuple) and len(node.elts) > 1 and isinstance(node.elts[0], ast.Constant) and node.elts[0].value in ('unresolved', 'accounted', 'reconciliation_required', 'externally_managed'):
            found |= reason_literals(node.elts[1])
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            # Literal SQL reason assignments and accounting state/reason VALUES pairs.
            found.update(re.findall(r"accounting_reason\s*=\s*'([^']+)'", node.value))
            if 'INSERT INTO print_archives' in node.value:
                found.update(re.findall(r"'(?:unresolved|accounted|reconciliation_required|externally_managed)',\s*'([^']+)'", node.value))
    return found


def test_accounting_sources_cannot_add_an_undocumented_reason():
    root = Path(__file__).resolve().parents[2] / 'backend/modules'
    found = set()
    for name in ('archives/archive.py', 'archives/attempt_accounting.py', 'archives/routes/projects.py', 'inventory/routes/filament_slots.py'):
        found |= collect_reasons((root / name).read_text())
    assert 'project_import' in found
    assert found <= REASONS, found - REASONS
    assert set(CAUSES) == REASONS


def test_reason_census_covers_whole_modules_and_sql_without_hand_entries():
    assert collect_reasons("""
def outside_create_archive():
    accounting_reason = 'new_python_reason' if state == 'not_a_reason' else 'other_reason'
    run("UPDATE print_archives SET accounting_reason='new_sql_reason'")
    return 'unresolved', 'new_return_reason', []
""") == {'new_python_reason', 'other_reason', 'new_sql_reason', 'new_return_reason'}


@pytest.mark.parametrize('reason', sorted(REASONS))
def test_every_catalogued_reason_has_a_customer_explanation(reason):
    result = describe(reason)
    assert result['code'] == reason and result['title'] and result['next_step']
    assert '_' not in result['title'] + result['next_step']


@pytest.mark.parametrize('reason,title', [
    ('no_bound_spool', 'No spool was bound to the slot this print used'),
    ('spool_change_pending', 'The spool in the slot changed and was not confirmed'),
    ('unknown_spool_balance', 'The spool has no known balance'),
    ('non_successful_attempt_quantity_unconfirmed', 'The print did not finish and the printer reported no amount'),
    ('first_seen_mid_print', 'ODIN did not see the whole print'),
    ('job_usage_unattributed', 'Filament was already deducted another way'),
    ('inventory_shortfall', 'The spool had less filament than the print used'),
    ('missing_file', DEFAULT[0]),
])
def test_cause_table(reason, title):
    assert describe(reason)['title'] == title


@pytest.mark.parametrize('source', ['missing_job_file', 'source_graph_changed', 'conflicting_education'])
def test_ownership_causes(source):
    expected = 'The print was started at the printer, not from an ODIN job' if source != 'conflicting_education' else "The print's job, file or organisation records disagree"
    assert describe('archive_ownership_unresolved', source)['title'] == expected


def test_list_filter_and_summary_window_visibility_and_education(settlement_case):
    case = settlement_case
    now = datetime.utcnow()
    write(case, 'UPDATE print_archives SET completed_at=:at', {'at': now - timedelta(days=1)})
    client = case[0]
    listed = client.get('/archives?accounting=unresolved').json()
    assert listed['total'] == 1 and listed['items'][0]['accounting_cause']['title']
    assert client.get('/archives?accounting=reconciliation_required').json()['total'] == 0
    assert client.get('/archives?accounting=invalid').status_code == 422
    summary = client.get('/archives/accounting-summary?days=30')
    assert summary.status_code == 200, summary.text
    assert summary.json()['unresolved'] == 1
    assert summary.json()['by_cause'][0]['count'] == 1
    write(case, 'UPDATE print_archives SET completed_at=:at', {'at': now - timedelta(days=31)})
    assert client.get('/archives/accounting-summary').json()['unresolved'] == 0
    write(case, 'UPDATE print_archives SET completed_at=:at,org_id=2', {'at': now - timedelta(days=1)})
    assert client.get('/archives/accounting-summary').json()['unresolved'] == 0
    write(case, 'UPDATE print_archives SET org_id=1,education_submission_id=-1')
    assert client.get('/archives/accounting-summary').json()['unresolved'] == 0
    assert client.get('/archives?accounting=unresolved').json()['total'] == 0
