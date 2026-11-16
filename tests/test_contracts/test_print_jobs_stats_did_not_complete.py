"""Combined status uses the same date, scope and paging path as exact statuses."""
import pytest
from sqlalchemy import text
from core.physical_print import CLOSED_UNKNOWN
from core.restart_questions import ENDED_UNKNOWN
from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db, interactive_headers
from tests.test_contracts.test_print_jobs_paging_and_dates import history_client, read


@pytest.fixture
def outcomes(history_client, review_db):
    for identifier, status, ended, code in (
        (910, 'cancelled', '2026-11-01 05:00:00', None),
        (911, 'cancelled', None, CLOSED_UNKNOWN),
        (912, 'cancelled', None, ENDED_UNKNOWN),
        (913, 'failed', None, None),
    ):
        review_db.execute(text("INSERT INTO print_jobs(id,printer_id,status,started_at,ended_at,error_code) "
            "VALUES(:id,9,:status,'2026-11-01 04:00:00',:ended,:code)"),
            dict(id=identifier, status=status, ended=ended, code=code))
    review_db.commit()
    return history_client


def test_stats_split_and_unknown_exclusion(outcomes):
    response = outcomes.get('/print-jobs/stats', headers=interactive_headers())
    assert response.status_code == 200, response.text
    row = next(r for r in response.json() if r['printer_id'] == 9)
    assert (row['completed_jobs'], row['failed_jobs'], row['stopped_jobs'], row['did_not_complete_jobs'], row['running_jobs']) == (3, 1, 1, 2, 1)


def test_combined_filter_counts_before_paging_and_intersects_dates(outcomes):
    for offset, ids in ((0, [910]), (1, [903]), (2, [])):
        response = read(outcomes, status='did_not_complete', limit=1, offset=offset, printer_id=9,
                        started_after='2026-11-01', ended_before='2026-11-02')
        assert response.headers['X-Total-Count'] == '2'
        assert [r['id'] for r in response.json()] == ids
    assert [r['id'] for r in read(outcomes, status='did_not_complete', ended_before='2026-11-01T04:00:00Z').json()] == [903]
    assert read(outcomes, status='unknown').json() == []
    assert {r['id'] for r in read(outcomes, status='cancelled').json()} == {910, 911, 912}
    assert {r['id'] for r in read(outcomes, status='failed').json()} == {903, 913}


def test_combined_filter_keeps_tenant_and_controlled_visibility(outcomes, review_db):
    review_db.execute(text("INSERT INTO printers(id,name,org_id) VALUES(199,'Other printer',2)"))
    review_db.execute(text("INSERT INTO print_jobs(printer_id,status,started_at,ended_at) VALUES(199,'cancelled',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
    review_db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id) VALUES(199,'Controlled stopped','cancelled',1)"))
    review_db.execute(text("INSERT INTO controlled_audiences(id,name) VALUES(901,'Private audience')"))
    review_db.execute(text("INSERT INTO controlled_jobs(job_id,label,audience_id,printer_tag,retention_days,reason,changed_by,marked_at) VALUES(199,'Restricted',901,'Secure',7,'Synthetic test',3,CURRENT_TIMESTAMP)"))
    review_db.execute(text('UPDATE print_jobs SET scheduled_job_id=199 WHERE id=910'))
    review_db.commit()
    response = read(outcomes, status='did_not_complete')
    assert [r['id'] for r in response.json()] == [903]
    assert response.headers['X-Total-Count'] == '1'
