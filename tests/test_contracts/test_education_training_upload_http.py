"""Multipart refusal is the public, named training error (not an internal 500)."""
from uuid import uuid4
from sqlalchemy import text
from tests.test_contracts.test_education_rejected_retry_http import retry_case
from tests.test_contracts.test_education_submission_upload import submission_db
from tests.test_contracts.test_license_user_limits import signed_license
from tests.test_contracts.test_education_training import create


def test_upload_returns_named_training_required_envelope(retry_case):
    db, client, auth, upload = retry_case
    db.execute(text("INSERT INTO printers(id,name,org_id,is_active) VALUES(9,'School printer',1,TRUE)"))
    db.execute(text("INSERT INTO education_cost_center_printers(org_id,cost_center_id,printer_id,state,granted_by) VALUES(1,7,9,'active',3)")); db.commit()
    training = create(db)
    response = upload(str(uuid4()))
    assert response.status_code == 403, response.text
    error = response.json()['error']
    assert error['code'] == 'training_required'
    assert error['required_trainings'] == [{'id': training['id'], 'name': 'P1S safety', 'status': 'required'}]
    assert db.execute(text('SELECT count(*) FROM jobs')).scalar_one() == 0
