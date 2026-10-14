"""Actual general archive/job responses do not project private unit inputs.

Audience identity uses the existing substituted archive fixture; this verifies
serialization isolation, not current financial authentication.
"""
import json
import pytest
from sqlalchemy import text
from tests.test_contracts.test_archive_reprint_behavior import reprint_client
from tests.test_contracts.test_education_review_workflow import review_db


@pytest.mark.parametrize('endpoint',['/archives/101','/archives','/archives/log','/archives/log/export','/jobs/101','/jobs'])
def test_general_audience_has_no_private_snapshot_extension(reprint_client,review_db,endpoint):
    client,identity,_=reprint_client
    identity.update(role='admin',group_id=1)
    payload=json.dumps({'version':1,'unit_valuation':{'price_decimal':'91827.3645','private_basis_marker':'owned-private-extension'}})
    identifier=review_db.execute(text("INSERT INTO print_jobs(printer_id,job_name,filename,scheduled_job_id,status,started_at,spool_snapshot) VALUES(9,'owned','owned',101,'completed',CURRENT_TIMESTAMP,:payload) RETURNING id"),{'payload':payload}).scalar_one()
    review_db.execute(text('UPDATE print_archives SET print_job_id=:attempt WHERE id=101'),{'attempt':identifier})
    review_db.commit()
    result=client.get(endpoint)
    assert result.status_code==200,result.text
    for private in ('unit_valuation','91827.3645','owned-private-extension','spool_snapshot'):
        assert private not in result.text
