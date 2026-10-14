"""Known and unknown report facts retain their distinct meaning."""
from datetime import datetime,timedelta,timezone
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db
from core.base import JobStatus
from core.economic_identity import _add_created
from modules.jobs.models import Job
from modules.reporting.education_usage import build_education_usage_report


def job(db,actual,estimated,quantity=1,printer=None):
    end=datetime.now(timezone.utc).replace(tzinfo=None)-timedelta(hours=1)
    value=Job(item_name='Owned metric',status=JobStatus.COMPLETED,submitted_by=1,
        charged_to_org_id=1,quantity=quantity,duration_hours=estimated,
        actual_start=end-timedelta(hours=actual) if actual else None,actual_end=end if actual else None,
        scheduled_start=end-timedelta(hours=estimated) if estimated else None,
        scheduled_end=end if estimated else None,printer_id=printer,estimated_cost=5)
    return _add_created(db,value,'job',owner_org_id=1)


def report(db,platform=False):
    return build_education_usage_report(30,db,{'id':3,'role':'admin','group_id':None if platform else 1})


def test_actual_hours_count_once_and_accuracy_mean_uses_unrounded_samples(review_db):
    job(review_db,2,1,4);job(review_db,3,1);review_db.commit()
    result=report(review_db)['summary']
    assert result['total_jobs']==2 and result['total_print_hours']==5
    assert result['estimated_print_hours']==5
    assert result['accuracy']==41.7 and result['accuracy_samples']==2
    assert result['total_cost'] is None and result['currency'] is None


def test_missing_intervals_are_unknown_and_never_perfect_accuracy(review_db):
    job(review_db,None,None);review_db.commit()
    result=report(review_db)['summary']
    assert result['accuracy'] is None and result['accuracy_samples']==0
    assert result['unknown_accuracy_jobs']==result['unknown_actual_hours_jobs']==1
    assert result['unknown_estimated_hours_jobs']==1


def test_platform_unproven_history_is_count_only(review_db):
    review_db.execute(text("INSERT INTO jobs(item_name,status,submitted_by,charged_to_org_id,duration_hours,quantity) VALUES('Legacy','completed',1,1,99,3)"));review_db.commit()
    result=report(review_db,True)['summary']
    assert result['unproven_history_jobs']>=1
    assert result['total_print_hours']==result['estimated_print_hours']==result['estimated_filament_grams']==0
    assert result['accuracy'] is None and result['total_cost'] is None


def test_foreign_printer_metadata_is_one_opaque_bucket(review_db):
    review_db.execute(text("INSERT INTO printers(id,name,org_id,shared) VALUES(77,'Foreign secret name',2,0),(78,'Second foreign secret',2,0)"))
    job(review_db,1,1,printer=77);job(review_db,1,1,printer=78);review_db.commit()
    result=report(review_db)
    assert len(result['by_printer'])==1
    group=result['by_printer'][0]
    assert group['id'] is None and group['name'] is None and group['available'] is False
    assert group['total_jobs_submitted']==2
    assert 'Foreign secret' not in str(result) and 'Second foreign secret' not in str(result)


def test_foreign_model_estimates_are_not_derived_metric_authority(review_db):
    identifier=review_db.execute(text('''INSERT INTO models(name,org_id,build_time_hours,color_requirements)
      VALUES('Foreign model secret',2,777,:requirements) RETURNING id'''),{'requirements':'{"black":{"grams":888}}'}).scalar_one()
    value=job(review_db,1,None)
    value.model_id=identifier;review_db.commit()
    result=report(review_db)
    assert 'Foreign model secret' not in str(result)
    assert result['summary']['estimated_print_hours']==0
    assert result['summary']['estimated_filament_grams']==0
    assert result['summary']['unknown_estimated_hours_jobs']==1
    assert result['summary']['unknown_filament_estimate_jobs']==1


def test_finite_estimates_overflow_to_explicit_unknown_in_all_aggregates(review_db):
    a=job(review_db,1,1);b=job(review_db,1,1)
    a.duration_hours=b.duration_hours=1e308
    review_db.commit()
    result=report(review_db)
    for bucket in [result['summary'],*result['users'],*result['by_printer'],*result['by_model']]:
        assert bucket['estimated_print_hours'] is None
        assert bucket['estimated_print_hours_unavailable_reason']=='numeric_overflow'
    import json
    json.dumps(result,allow_nan=False)


def test_finite_filament_estimates_overflow_without_nonfinite_response(review_db):
    identifier=review_db.execute(text('''INSERT INTO models(name,org_id,color_requirements)
      VALUES('Owned huge estimate',1,:requirements) RETURNING id'''),{'requirements':'{"black":{"grams":1e308}}'}).scalar_one()
    a=job(review_db,1,1);b=job(review_db,1,1)
    a.model_id=b.model_id=identifier;review_db.commit()
    result=report(review_db)
    for bucket in [result['summary'],*result['users'],*result['by_printer'],*result['by_model']]:
        assert bucket['estimated_filament_grams'] is None
        assert bucket['estimated_filament_grams_unavailable_reason']=='numeric_overflow'
    import json
    json.dumps(result,allow_nan=False)
