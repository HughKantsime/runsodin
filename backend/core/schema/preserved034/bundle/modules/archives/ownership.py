"""Content ownership from authoritative job/file references, never device sharing."""
from .attempt_accounting import execute


def capture_content_owner(connection, scheduled_job_id):
    unknown = {'state': 'unresolved', 'org_id': None, 'source': 'missing_job_file',
               'scheduled_job_id': scheduled_job_id, 'print_file_id': None,
               'education_submission_id': None}
    if scheduled_job_id is None:
        return unknown
    job = execute(connection, 'SELECT print_file_id,charged_to_org_id,model_id FROM jobs WHERE id=?',
                  (scheduled_job_id,)).fetchone()
    if job is None:
        return unknown
    if job[0] is not None:
        files = execute(connection, 'SELECT id,org_id,model_id,stored_path FROM print_files WHERE id=?', (job[0],)).fetchall()
    else:
        files = execute(connection, 'SELECT id,org_id,model_id,stored_path FROM print_files WHERE job_id=? ORDER BY id',
                        (scheduled_job_id,)).fetchall()
    if len(files) != 1:
        return unknown
    file = files[0]
    unknown['print_file_id'] = file[0]
    if job[2] is not None and file[2] != job[2]:
        return dict(unknown, source='conflicting_model')
    owners = {value for value in (job[1], file[1]) if value is not None}
    if job[2] is not None:
        model = execute(connection, 'SELECT org_id FROM models WHERE id=?', (job[2],)).fetchone()
        if model is None:
            return dict(unknown, source='missing_model')
        if model[0] is not None:
            owners.add(model[0])
    if len(owners) > 1:
        return dict(unknown, source='conflicting_tenant')
    org = next(iter(owners), None)
    education = execute(connection,
        'SELECT id,org_id,job_id,print_file_id,model_id,operation_id,submitted_by FROM education_submissions WHERE job_id=? OR print_file_id=?',
        (scheduled_job_id, file[0])).fetchall()
    operations = execute(connection,
        'SELECT operation_id,org_id,user_id,state FROM education_upload_operations WHERE final_path=? AND final_path IS NOT NULL',
        (file[3],)).fetchall()
    marker = None
    if operations and not education:
        return dict(unknown, source='education_operation_without_submission', education_submission_id=-1)
    if education:
        # Retain a marker even for a corrupt/ambiguous Education source graph.
        marker = education[0][0] if len(education) == 1 else -1
        if len(education) != 1 or tuple(education[0][1:5]) != (org, scheduled_job_id, file[0], job[2]):
            return dict(unknown, source='conflicting_education', education_submission_id=marker)
        complete_graph = execute(connection,
            'SELECT s.id FROM education_submissions s '
            'JOIN jobs j ON j.id=s.job_id AND j.charged_to_org_id=s.org_id AND j.model_id=s.model_id AND j.submitted_by=s.submitted_by '
            'JOIN print_files f ON f.id=s.print_file_id AND f.org_id=s.org_id AND f.model_id=s.model_id AND f.created_by=s.submitted_by '
            'JOIN models m ON m.id=s.model_id AND m.org_id=s.org_id AND m.print_file_id=f.id '
            'JOIN users u ON u.id=s.submitted_by AND u.group_id=s.org_id '
            'JOIN education_cost_centers c ON c.id=s.cost_center_id AND c.org_id=s.org_id '
            'JOIN education_upload_operations o ON o.operation_id=s.operation_id AND o.org_id=s.org_id AND o.user_id=s.submitted_by '
            "AND o.final_path=f.stored_path AND o.state='committed' WHERE s.id=?",
            (marker,)).fetchone()
        if complete_graph is None:
            return dict(unknown, source='incomplete_education_graph', education_submission_id=marker)
        expected_operation = (education[0][5], org, education[0][6], 'committed')
        if len(operations) != 1 or tuple(operations[0]) != expected_operation:
            return dict(unknown, source='conflicting_education_operation', education_submission_id=marker)
    return {'state': 'known' , 'org_id': org, 'source': 'job_file',
            'scheduled_job_id': scheduled_job_id, 'print_file_id': file[0],
            'education_submission_id': marker}
