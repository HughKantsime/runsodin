"""Known-value report HTML from real isolated database queries."""
import re
import pytest
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db


def plain(html):return re.sub(r'\s+',' ',re.sub('<[^>]+>',' ',html))


def test_fleet_report_known_utilization_and_hours(review_db):
    from modules.reporting.report_runner import generate_fleet_utilization
    review_db.execute(text("UPDATE printers SET is_active=1,total_print_hours=12,gcode_state='IDLE'"))
    review_db.execute(text("UPDATE printers SET gcode_state='RUNNING' WHERE id=9"));review_db.commit()
    html=plain(generate_fleet_utilization(review_db,{}))
    assert 'TOTAL PRINTERS 2' in html and 'ACTIVE 2' in html and 'CURRENTLY PRINTING 1' in html and 'UTILIZATION 50.0%' in html and 'TOTAL PRINT HOURS 24.0' in html


def test_filament_report_aggregates_actual_usage(review_db):
    from modules.reporting.report_runner import generate_filament_consumption
    review_db.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(199,'Synthetic','Synthetic stock','SYNTHETIC')"))
    review_db.execute(text('INSERT INTO spools(id,filament_id) VALUES(199,199)'))
    review_db.execute(text('INSERT INTO spool_usage(spool_id,weight_used_g,used_at) VALUES(199,20,CURRENT_TIMESTAMP),(199,30,CURRENT_TIMESTAMP)'));review_db.commit()
    html=plain(generate_filament_consumption(review_db,{}))
    assert 'SYNTHETIC 50.0g' in html


def test_job_summary_and_failure_reports_known_rows(review_db):
    from modules.reporting.report_runner import generate_job_summary,generate_failure_analysis
    review_db.execute(text("INSERT INTO jobs(item_name,status,quantity,fail_reason,created_at) VALUES('Synthetic completed','completed',1,NULL,CURRENT_TIMESTAMP),('Synthetic failed','failed',1,'Synthetic reason',CURRENT_TIMESTAMP),('Synthetic failed','failed',1,'Synthetic reason',CURRENT_TIMESTAMP)"));review_db.commit()
    assert 'failed 2' in plain(generate_job_summary(review_db,{}))
    assert 'completed 1' in plain(generate_job_summary(review_db,{}))
    assert 'Synthetic reason 2' in plain(generate_failure_analysis(review_db,{}))


def test_chargeback_report_current_stored_values(review_db):
    from modules.reporting.report_runner import generate_chargeback_summary
    # Isolate the known-value sample from the shared Education fixture's submitted job.
    review_db.execute(text('UPDATE jobs SET charged_to_user_id=NULL'))
    review_db.execute(text("INSERT INTO jobs(item_name,status,quantity,estimated_cost,duration_hours,charged_to_user_id,created_at) VALUES('Synthetic charged','completed',1,5,2,1,CURRENT_TIMESTAMP),('Synthetic charged','completed',1,7,3,1,CURRENT_TIMESTAMP)"));review_db.commit()
    # No charged org / configured currency: the amount carries no presumed '$' (F10-10.9-02).
    assert 'student 2 12.00 5.0h' in plain(generate_chargeback_summary(review_db,{}))



def test_report_generator_escapes_user_controlled_markup(review_db):
    from modules.reporting.report_runner import generate_chargeback_summary
    review_db.execute(text('UPDATE jobs SET charged_to_user_id=NULL'))
    review_db.execute(text("UPDATE users SET username='<img src=x>' WHERE id=1"))
    review_db.execute(text("INSERT INTO jobs(item_name,status,quantity,estimated_cost,duration_hours,charged_to_user_id,created_at) VALUES('Synthetic markup','completed',1,5,2,1,CURRENT_TIMESTAMP)"));review_db.commit()
    html=generate_chargeback_summary(review_db,{})
    assert '<img src=x>' not in html
    assert '&lt;img src=x&gt;' in html
