"""D2 rows for the existing current-schema PostgreSQL backup/restore proof."""
from sqlalchemy import text


def seed(connection):
    connection.execute(text("INSERT INTO groups(id,name,is_org) VALUES(990048,'D2 roundtrip',TRUE)"))
    connection.execute(text("INSERT INTO users(id,username,password_hash,role,group_id,is_active) VALUES(990048,'d2-roundtrip','fixture','viewer',990048,TRUE)"))
    connection.execute(text('INSERT INTO education_memberships(user_id,org_id) VALUES(990048,990048) ON CONFLICT DO NOTHING'))
    connection.execute(text("INSERT INTO education_cost_centers(id,org_id,name_key,code_key,display_name,code,created_by) VALUES(990048,990048,'d2','d2','D2','d2',990048)"))
    connection.execute(text("INSERT INTO education_classes(id,org_id,cost_center_id,title,term,created_by) VALUES('roundtrip048-class',990048,990048,'Class','Fall',990048)"))
    connection.execute(text("INSERT INTO education_assignments(id,org_id,class_id,title,brief,allowed_materials,auto_printer_ids,authorized_by) VALUES('roundtrip048-assignment',990048,'roundtrip048-class','Gear','Brief','[]','[]',990048)"))
    connection.execute(text("INSERT INTO education_upload_operations(operation_id,org_id,user_id,state) VALUES('roundtrip048',990048,990048,'committed')"))
    connection.execute(text("INSERT INTO print_files(id,filename,org_id,created_by) VALUES(990048,'gear.3mf',990048,990048)"))
    connection.execute(text("INSERT INTO models(id,name,print_file_id,org_id) VALUES(990048,'Gear',990048,990048)"))
    connection.execute(text("INSERT INTO jobs(id,item_name,model_id,status,submitted_by,charged_to_user_id,charged_to_org_id) VALUES(990048,'Gear',990048,'submitted',990048,990048,990048)"))
    connection.execute(text("INSERT INTO education_submissions(id,org_id,operation_id,job_id,print_file_id,model_id,cost_center_id,submitted_by,status) VALUES(990048,990048,'roundtrip048',990048,990048,990048,990048,990048,'submitted')"))
    connection.execute(text("INSERT INTO education_assignment_submissions(submission_id,org_id,assignment_id,feedback,grade,reviewed_by) VALUES(990048,990048,'roundtrip048-assignment','Retained feedback','A',990048)"))

    connection.execute(text("INSERT INTO education_assessment_revisions(id,submission_id,org_id,revision,reviewed_by,old_grade,new_grade,old_feedback,new_feedback,old_learning_status,new_learning_status) VALUES('roundtrip048-revision',990048,990048,2,990048,'','A','','Retained feedback','submitted','reviewed')"))


def verify(connection):
    assert connection.execute(text("SELECT title,term FROM education_classes WHERE id='roundtrip048-class'")).one() == ('Class', 'Fall')
    assert connection.execute(text("SELECT title,brief FROM education_assignments WHERE id='roundtrip048-assignment'")).one() == ('Gear', 'Brief')
    assert connection.execute(text('SELECT feedback,grade FROM education_assignment_submissions WHERE submission_id=990048')).one() == ('Retained feedback', 'A')

    assert connection.execute(text("SELECT old_grade,new_grade,new_feedback FROM education_assessment_revisions WHERE id='roundtrip048-revision'")).one() == ('', 'A', 'Retained feedback')
