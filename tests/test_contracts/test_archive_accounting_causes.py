"""Reason vocabulary census, plain explanations and visible archive aggregation."""
import ast
import re
from pathlib import Path
from datetime import datetime, timedelta
import pytest
from tests.test_contracts.test_archive_filament_settlement import settlement_case, archive_case, review_db, write  # noqa: F401
from modules.archives.accounting_causes import CAUSES, DEFAULT, REASONS, describe


def strings(node):
    return {n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def test_accounting_sources_cannot_add_an_undocumented_reason():
    root = Path(__file__).resolve().parents[2] / 'backend/modules/archives'
    found = set()
    for name in ('archive.py', 'attempt_accounting.py', 'routes/projects.py'):
        tree = ast.parse((root / name).read_text())
        scope = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'create_print_archive') if name == 'archive.py' else tree
        for node in ast.walk(scope):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'unresolved':
                for arg in node.args: found |= strings(arg)
            if isinstance(node, ast.keyword) and node.arg == 'reason':
                found |= strings(node.value)
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value == 'reason': found |= strings(value)
            if isinstance(node, ast.Assign) and any(isinstance(n, ast.Name) and n.id == 'reason' for target in node.targets for n in ast.walk(target)):
                if isinstance(node.value, ast.Tuple):
                    for value in node.value.elts[1:2]: found |= strings(value)
                elif not isinstance(node.value, ast.Call): found |= strings(node.value)
            if isinstance(node, ast.Tuple) and node.elts and isinstance(node.elts[0], ast.Constant) and node.elts[0].value in ('unresolved', 'accounted', 'reconciliation_required', 'externally_managed'):
                found |= strings(node.elts[1])
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and 'INSERT INTO print_archives' in node.value and 'accounting_reason' in node.value:
                found.update(re.findall(r"'unresolved',\s*'([^']+)'", node.value))
    # The import route embeds its reason in a bound tuple rather than an accounting helper.
    assert "'project_import'" in (root / 'routes/projects.py').read_text()
    found.add('project_import')
    # Conditions in ternary expressions include state/metadata lookup strings, not reasons.
    found -= {'state', 'complete', 'reason', 'identity_state', 'price'}
    assert found <= REASONS, found - REASONS
    assert set(CAUSES) == REASONS


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
