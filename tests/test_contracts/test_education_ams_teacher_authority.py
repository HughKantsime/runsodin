"""Real current grant/revision/ownership checks before read generation mutation."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db,_principal
from modules.organizations.education_ams_teacher_authority import lock_teacher_ams_context,EducationAMSAuthorityUnavailable


def test_manager_context_matches_normal_review_and_rolls_back(review_db):
    context=lock_teacher_ams_context(review_db,principal=_principal(2),submission_id=14,printer_id=9,revision=1)
    assert (context.submission_id,context.job_id,context.org_id,context.cost_center_id)==(14,13,1,7)
    assert review_db.in_transaction()
    review_db.rollback()


@pytest.mark.parametrize('mutation',[
    "UPDATE education_cost_center_grants SET state='revoked' WHERE user_id=2",
    "UPDATE education_cost_centers SET state='archived' WHERE id=7",
    "UPDATE education_cost_center_printers SET state='revoked' WHERE printer_id=9",
    'UPDATE education_submissions SET lifecycle_revision=2 WHERE id=14',
    'UPDATE users SET is_active=0 WHERE id=2',
    'UPDATE printers SET shared=1 WHERE id=9'])
def test_completed_authority_change_refuses(review_db,mutation):
    lock_teacher_ams_context(review_db,principal=_principal(2),submission_id=14,printer_id=9,revision=1)
    review_db.rollback()
    review_db.execute(text(mutation));review_db.commit()
    with pytest.raises(EducationAMSAuthorityUnavailable):
        lock_teacher_ams_context(review_db,principal=_principal(2),submission_id=14,printer_id=9,revision=1)
    review_db.rollback()


def test_stale_admin_principal_cannot_preserve_removed_admin_authority(review_db):
    principal=_principal(3,role='admin')
    review_db.execute(text("UPDATE users SET role='viewer' WHERE id=3"));review_db.commit()
    with pytest.raises(EducationAMSAuthorityUnavailable):
        lock_teacher_ams_context(review_db,principal=principal,submission_id=14,printer_id=9,revision=1)
    review_db.rollback()


def test_foreign_printer_cannot_be_selected(review_db):
    review_db.execute(text("INSERT INTO printers(id,name,is_active,shared,org_id) VALUES(11,'Foreign test printer',1,0,2)"))
    review_db.commit()
    with pytest.raises(EducationAMSAuthorityUnavailable):
        lock_teacher_ams_context(review_db,principal=_principal(2),submission_id=14,printer_id=11,revision=1)
    review_db.rollback()
