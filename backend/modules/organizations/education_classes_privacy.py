"""GDPR integration stays available even after an Education entitlement expires."""
from contextlib import contextmanager
import importlib
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from core.identity_scrub import scrub_text


def export_classes(db, user_id):
    def rows(sql):
        return [dict(r) for r in db.execute(text(sql), {'u': user_id}).mappings()]
    return {
        'classes': rows('SELECT DISTINCT c.* FROM education_classes c LEFT JOIN education_cost_center_grants g '
            'ON g.cost_center_id=c.cost_center_id AND g.org_id=c.org_id WHERE g.user_id=:u OR c.created_by=:u'),
        'assignments_authored': rows('SELECT * FROM education_assignments WHERE authorized_by=:u'),
        'assessments': rows('SELECT x.* FROM education_assignment_submissions x JOIN education_submissions s '
            'ON s.id=x.submission_id AND s.org_id=x.org_id WHERE s.submitted_by=:u'),
        'assessment_revisions': rows('SELECT x.* FROM education_assessment_revisions x JOIN education_submissions s '
            'ON s.id=x.submission_id AND s.org_id=x.org_id WHERE s.submitted_by=:u ORDER BY x.created_at,x.submission_id,x.revision,x.id'),
        # Teacher exports describe their reviewer role, never their students' marks/text.
        'assessments_reviewed': rows("SELECT x.submission_id,x.org_id,x.assignment_id,x.reviewed_by,'reviewer' AS role "
            'FROM education_assignment_submissions x WHERE x.reviewed_by=:u'),
        'assessment_revisions_reviewed': rows("SELECT id,submission_id,org_id,revision,reviewed_by,created_at,'reviewer' AS role "
            'FROM education_assessment_revisions WHERE reviewed_by=:u ORDER BY created_at,submission_id,revision,id'),
        'actions': rows("SELECT * FROM education_audit_events WHERE actor_kind='user' AND actor_id=CAST(:u AS TEXT) "
            "AND (action LIKE 'class.%' OR action LIKE 'assignment.%') ORDER BY created_at,event_id"),
    }


_SCRUB_UPDATES = {
    'education_classes': 'UPDATE education_classes SET title=:title,term=:term,revision=revision+1 WHERE id=:key',
    'education_assignments': 'UPDATE education_assignments SET title=:title,brief=:brief,revision=revision+1 WHERE id=:key',
    'education_assignment_submissions': 'UPDATE education_assignment_submissions SET feedback=:feedback,grade=:grade,revision=revision+1 WHERE submission_id=:key',
    'education_assessment_revisions': 'UPDATE education_assessment_revisions SET old_feedback=:old_feedback,new_feedback=:new_feedback,old_grade=:old_grade,new_grade=:new_grade WHERE id=:key',
}

def _scrub_rows(db, table, key, fields, identifiers):
    """Bound both result buffering and Python work; LIKE is only a broad prefilter.

    Exact identifier boundaries are enforced by scrub_text. Escaped LIKE values
    prevent usernames containing '%' or '_' from turning into table-wide matches.
    Tables/columns come only from the closed inventory below, never a request.
    """
    ids = sorted({v for v in identifiers if isinstance(v, str) and v})
    for start in range(0, len(ids), 20):
        params = {f'p{i}': '%' + value.replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'
                  for i, value in enumerate(ids[start:start + 20])}
        predicate = ' OR '.join(f"LOWER({f}) LIKE LOWER(:{p}) ESCAPE '!'" for f in fields for p in params)
        after = None
        while True:
            paging = '' if after is None else f' AND {key}>:after'
            rows = db.execute(text('SELECT ' + key + ',' + ','.join(fields) + ' FROM ' + table +
                ' WHERE (' + predicate + ')' + paging + f' ORDER BY {key} LIMIT 200'),
                {**params, 'after': after}).mappings().all()
            if not rows:
                break
            for row in rows:
                values = {f: scrub_text(row[f], identifiers) for f in fields}
                if any(values[f] != row[f] for f in fields):
                    db.execute(text(_SCRUB_UPDATES[table]),
                        {**values, 'key': row[key]})
            after = rows[-1][key]


def _lock_erased_assessments(db, user_id, identifiers):
    # Match every current assessment that erasure may update, including mentions
    # in another student's feedback. Lock in primary-key order before history DDL.
    params = {'u': user_id}
    predicates = ['reviewed_by=:u', 'submission_id IN '
                  '(SELECT id FROM education_submissions WHERE submitted_by=:u)']
    for i, value in enumerate(sorted({v for v in identifiers if isinstance(v, str) and v})):
        key = f'p{i}'
        params[key] = '%' + value.replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'
        predicates.extend(f"LOWER({field}) LIKE LOWER(:{key}) ESCAPE '!'" for field in ('feedback', 'grade'))
    db.execute(text('SELECT submission_id FROM education_assignment_submissions WHERE (' +
        ' OR '.join(predicates) + ') ORDER BY submission_id FOR UPDATE'), params).all()


@contextmanager
def _assessment_erasure(db, user_id, identifiers):
    """Temporarily allow GDPR updates under a transaction-held exclusive write lock.

    Trigger DDL and scrubbing share a savepoint: failure restores both history and
    its guard. Other connections cannot write while the update guard is absent.
    DELETE (and replacement/TRUNCATE) protection remains installed throughout.
    """
    connection = db.connection()
    sqlite = connection.dialect.name == 'sqlite'
    if sqlite and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
    with db.begin_nested():
        # Materialize the Session savepoint before issuing DDL on its connection.
        connection = db.connection()
        if sqlite:
            connection.exec_driver_sql('DROP TRIGGER trg_assessment_revisions_no_update')
        else:
            _lock_erased_assessments(db, user_id, identifiers)
            connection.exec_driver_sql('ALTER TABLE education_assessment_revisions DISABLE TRIGGER trg_assessment_revisions_no_update')
        yield
        if sqlite:
            migration = importlib.import_module('core.schema.migrations.049_education_classes')
            connection.exec_driver_sql(migration.sqlite_triggers()['trg_assessment_revisions_no_update'])
        else:
            connection.exec_driver_sql('ALTER TABLE education_assessment_revisions ENABLE TRIGGER trg_assessment_revisions_no_update')


def erase_classes(db, user_id, identifiers):
    for attempt in range(2):
        try:
            with _assessment_erasure(db, user_id, identifiers):
                _erase_classes(db, user_id, identifiers)
            return
        except DBAPIError as exc:
            # Backstop for any remaining concurrent lock cycle. The context
            # restores the guard and scrubbed data before one bounded retry.
            if attempt or getattr(exc.orig, 'sqlstate', None) != '40P01':
                raise


def _erase_classes(db, user_id, identifiers):
    # A student's own marks are cleared; removing a teacher preserves the school
    # assessment and only removes reviewer identity and mentions in retained text.
    db.execute(text("UPDATE education_assignment_submissions SET feedback='',grade='',reviewed_by=NULL,revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE submission_id IN (SELECT id FROM education_submissions WHERE submitted_by=:u)"), {'u': user_id})
    db.execute(text('UPDATE education_assignment_submissions SET reviewed_by=NULL,revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE reviewed_by=:u'), {'u': user_id})
    db.execute(text("UPDATE education_assessment_revisions SET old_feedback='',new_feedback='',old_grade='',new_grade='',reviewed_by=NULL WHERE submission_id IN (SELECT id FROM education_submissions WHERE submitted_by=:u)"), {'u': user_id})
    db.execute(text('UPDATE education_assessment_revisions SET reviewed_by=NULL WHERE reviewed_by=:u'), {'u': user_id})
    db.execute(text("UPDATE education_assignments SET auto_printer_ids='[]',auto_print_seconds=NULL,"
        'auto_material_grams=NULL,revision=revision+1 WHERE authorized_by=:u'), {'u': user_id})
    # Operational school text may mention this person even when authored by someone else.
    for table, key, fields in (
        ('education_classes', 'id', ('title', 'term')),
        ('education_assignments', 'id', ('title', 'brief')),
        ('education_assignment_submissions', 'submission_id', ('feedback', 'grade')),
        ('education_assessment_revisions', 'id', ('old_feedback', 'new_feedback', 'old_grade', 'new_grade')),
    ):
        _scrub_rows(db, table, key, fields, identifiers)
