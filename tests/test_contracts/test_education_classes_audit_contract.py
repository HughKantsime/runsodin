"""The shared audit census must reject broken D2 caller-owned transactions."""
from pathlib import Path
import pytest
from tests.test_contracts.education_classes_audit_contract import FILES, classes_audit_is_atomic

ROOT = Path(__file__).resolve().parents[2] / 'backend/modules/organizations'


@pytest.mark.parametrize('mutation', [None, 'service_commit', 'link_commit', 'assessment_commit',
    'review_commit', 'automatic_review', 'helper_commit', 'helper_session'])
def test_classes_audit_transaction_contract(mutation):
    sources = {name: (ROOT / name).read_text() for name in FILES}
    files = {'service_commit': FILES[0], 'link_commit': FILES[1],
             'assessment_commit': FILES[2], 'review_commit': FILES[3]}
    if mutation in files:
        name = files[mutation]
        sources[name] = sources[name].replace('db.commit()', 'db.flush()')
    elif mutation == 'automatic_review':
        sources[FILES[1]] = sources[FILES[1]].replace('        approve_submission(db,', '        missing_review(db,')
    elif mutation == 'helper_commit':
        sources[FILES[0]] = sources[FILES[0]].replace('    log_audit(db,', '    db.commit()\n    log_audit(db,')
    elif mutation == 'helper_session':
        sources[FILES[0]] = sources[FILES[0]].replace('    log_audit(db,', '    log_audit(other_db,')
    assert classes_audit_is_atomic(sources) is (mutation is None)


@pytest.mark.parametrize('mutation', [None, 'jobs_table', 'status_column', 'static_update_target'])
def test_privacy_census_allowance_has_a_closed_table_and_field_inventory(mutation):
    from tests.test_contracts.education_classes_privacy_contract import privacy_writes_exclude_jobs
    source = (ROOT / 'education_classes_privacy.py').read_text()
    if mutation == 'jobs_table':
        source = source.replace("'education_classes'", "'jobs'")
    elif mutation == 'status_column':
        source = source.replace("('title', 'term')", "('status', 'term')")
    elif mutation == 'static_update_target':
        source = source.replace('UPDATE education_classes SET', 'UPDATE jobs SET')
    assert privacy_writes_exclude_jobs(source) is (mutation is None)
