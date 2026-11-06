"""Private portfolios: no direct filesystem URLs or borrowed media authority."""
from sqlalchemy import text
from modules.organizations.education_classes_service import actor, assignment_access, refused


_SELECT = '''SELECT s.id AS submission_id,s.job_id,s.print_file_id,s.cost_center_id,s.status,
    s.lifecycle_revision,s.created_at,j.item_name,u.username AS student_username,
    x.assignment_id,x.feedback,x.grade,x.learning_status,x.revision AS assessment_revision,
    a.title AS assignment_title,c.title AS class_title,c.term
    FROM education_submissions s
    JOIN jobs j ON j.id=s.job_id AND j.charged_to_org_id=s.org_id
    JOIN users u ON u.id=s.submitted_by
    LEFT JOIN education_assignment_submissions x ON x.submission_id=s.id AND x.org_id=s.org_id
    LEFT JOIN education_assignments a ON a.id=x.assignment_id AND a.org_id=x.org_id
    LEFT JOIN education_classes c ON c.id=a.class_id AND c.org_id=a.org_id
'''


def portfolio(db, principal, *, assignment_id=None, offset=0, limit=30):
    org, _ = actor(db, principal)
    manager = False
    if assignment_id:
        _, cls = assignment_access(db, principal, assignment_id)
        manager = cls['can_manage']
    assignment_filter = 'AND x.assignment_id=:a ' if assignment_id is not None else ''
    rows = db.execute(text(_SELECT + ' WHERE s.org_id=:o AND (:manager=1 OR s.submitted_by=:u) '
        + assignment_filter + 'ORDER BY s.created_at DESC,s.id DESC LIMIT :limit OFFSET :offset'),
        {'o': org, 'u': principal['id'], 'manager': int(manager), 'a': assignment_id,
         'limit': limit + 1, 'offset': offset}).mappings().all()
    items = []
    for row in rows[:limit]:
        media = db.execute(text('SELECT DISTINCT t.id FROM timelapses t JOIN print_archives ar '
            'ON ar.print_job_id=t.print_job_id AND ar.printer_id=t.printer_id '
            "WHERE ar.education_submission_id=:s AND ar.org_id=:o AND ar.ownership_state='known' "
            "AND t.status='ready' ORDER BY t.id"), {'s': row['submission_id'], 'o': org}).scalars().all()
        items.append({**row, 'thumbnail_url': f'/api/education/classes/portfolio/{row["submission_id"]}/thumbnail',
                      'timelapse_ids': media})
    return {'items': items, 'next_offset': offset + limit if len(rows) > limit else None}


def thumbnail(db, principal, submission_id):
    from core.print_payload_snapshot import stage_payload
    from core.zip_bounds import open_bounded
    from modules.organizations.education_review_service import assert_file_preview_allowed
    org, _ = actor(db, principal)
    row = db.execute(text('SELECT s.print_file_id,s.submitted_by,x.assignment_id FROM education_submissions s '
        'LEFT JOIN education_assignment_submissions x ON x.submission_id=s.id AND x.org_id=s.org_id '
        'WHERE s.id=:s AND s.org_id=:o'), {'s': submission_id, 'o': org}).first()
    if not row:
        raise refused('Thumbnail not found')
    if row.submitted_by != principal['id']:
        if not row.assignment_id:
            raise refused('Thumbnail not found')
        assignment_access(db, principal, row.assignment_id, manage=True)
    assert_file_preview_allowed(db, file_id=row.print_file_id, principal=principal)
    file = db.execute(text("SELECT stored_path,file_hash,storage_bytes FROM print_files WHERE id=:id AND org_id=:o AND blob_state='present'"),
                      {'id': row.print_file_id, 'o': org}).first()
    if not file:
        raise refused('Thumbnail unavailable')
    # Hash-bound snapshot and bounded PNG extraction; never serve arbitrary archive members.
    import os
    try:
        with stage_payload(file.stored_path, file.file_hash) as snapshot:
            if os.path.getsize(snapshot.path) != file.storage_bytes:
                raise ValueError()
            with open_bounded(snapshot.path) as archive:
                for name in ('Metadata/plate_1.png', 'Metadata/thumbnail.png'):
                    if name not in archive.namelist():
                        continue
                    member = archive.getinfo(name)
                    if member.file_size > 2 * 1024 * 1024:
                        raise ValueError()
                    data = archive.read(member)
                    if data.startswith(b'\x89PNG\r\n\x1a\n'):
                        return data
    except Exception:
        raise refused('Thumbnail unavailable') from None
    raise refused('Thumbnail unavailable')
