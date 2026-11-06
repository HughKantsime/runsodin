"""Fresh D2 fixtures for the shared real-HTTP permission matrix probe."""
def request(key, user, sql, users, sequence):
    i = next(sequence)
    owner = users['operator'] if user == 'super' else users[user]
    sql("INSERT INTO education_cost_centers(id,org_id,name_key,code_key,display_name,code,created_by) "
        "VALUES(:i,1,:name,:name,:name,:name,:u)", i=i, name=f'D2-{i}', u=owner)
    for role in ('manager', 'student'):
        sql("INSERT INTO education_cost_center_grants(org_id,cost_center_id,user_id,role,state,granted_by) "
            "VALUES(1,:i,:u,:role,'active',:u)", i=i, u=owner, role=role)
    cls, assignment = f'class-{i}', f'assignment-{i}'
    sql("INSERT INTO education_classes(id,org_id,cost_center_id,title,term,created_by) VALUES(:c,1,:i,'Class','Term',:u)", c=cls, i=i, u=owner)
    sql("INSERT INTO education_assignments(id,org_id,class_id,title,brief,allowed_materials,auto_printer_ids,authorized_by) "
        "VALUES(:a,1,:c,'Assignment','','[]','[]',:u)", a=assignment, c=cls, u=owner)
    if key == 'read':
        return 'get', '/api/education/classes', {}
    if key == 'manage':
        return 'post', f'/api/education/classes/{cls}/assignments', {'json': {'title': 'New assignment'}}
    sql("INSERT INTO education_upload_operations(operation_id,org_id,user_id,state) VALUES(:op,1,:u,'committed')", op=f'd2-{i}', u=owner)
    sql("INSERT INTO print_files(id,filename,org_id,created_by) VALUES(:i,'gear.3mf',1,:u)", i=i, u=owner)
    sql("INSERT INTO models(id,name,print_file_id,org_id) VALUES(:i,'Gear',:i,1)", i=i)
    sql("INSERT INTO jobs(id,item_name,status,model_id,submitted_by,charged_to_user_id,charged_to_org_id) VALUES(:i,'Gear','submitted',:i,:u,:u,1)", i=i, u=owner)
    sql("INSERT INTO education_submissions(id,org_id,operation_id,job_id,print_file_id,model_id,cost_center_id,submitted_by,status) "
        "VALUES(:i,1,:op,:i,:i,:i,:i,:u,'submitted')", i=i, op=f'd2-{i}', u=owner)
    path = f'/api/education/classes/assignments/{assignment}/submissions'
    if key == 'submit':
        return 'post', path, {'json': {'submission_id': i, 'revision': 1}}
    sql("INSERT INTO education_assignment_submissions(submission_id,org_id,assignment_id) VALUES(:i,1,:a)", i=i, a=assignment)
    return 'put', path + f'/{i}', {'json': {'revision': 1, 'learning_status': 'complete', 'grade': 'A'}}
