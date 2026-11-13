"""Transactional in-app restart questions, using the shared recipient policy.

These require an answer, so preference suppression and external delivery do not
apply. The question document owns the alert IDs and deduplicates across restarts.
"""
import json
import time
from sqlalchemy import text
from modules.notifications.recipient_policy import resolve_alert_scope


def stage(db, printer_id, printer_name, question):
    eligible, _ = resolve_alert_scope(db, printer_id=printer_id, job_id=question['scheduled_job_id'])
    recipients = db.execute(text("SELECT id FROM users WHERE is_active IS TRUE AND role IN ('admin','operator')")).all()
    message = f'{printer_name} was printing {question["print_name"]} when ODIN restarted. Confirm whether it is still the same print.'
    metadata = json.dumps({'restart_question_id': question['id'], 'print_job_id': question['print_job_id']})
    identifiers = []
    user_ids = []
    for (user_id,) in recipients:
        if user_id not in eligible:
            continue
        identifier = db.execute(text('INSERT INTO alerts(user_id,alert_type,severity,title,message,printer_id,metadata_json,is_read,is_dismissed,created_at) '
                                     "VALUES(:user,'printer_error','warning',:title,:message,:printer,:metadata,FALSE,FALSE,CURRENT_TIMESTAMP) RETURNING id"),
                                {'user': user_id, 'title': 'Confirm the print after restart', 'message': message,
                                 'printer': printer_id, 'metadata': metadata}).scalar_one()
        identifiers.append(identifier)
        user_ids.append(user_id)
    event(db, "alert_new", printer_id, user_ids, title="Confirm the print after restart", message=message,
          restart_question_id=question["id"])
    return identifiers


def event(db, kind, printer_id, user_ids, **fields):
    # The same durable WebSocket envelope used by ws_hub, in the question's
    # transaction so a restart cannot lose or duplicate the in-app notification.
    payload = {'type': kind, 'data': {'printer_id': printer_id, **fields}, '_audience': {'user_ids': user_ids}}
    db.execute(text('INSERT INTO ws_events(event_type,data,created_at) VALUES(:kind,:data,:at)'),
               {'kind': kind, 'data': json.dumps(payload), 'at': time.time()})


def resolve(db, identifiers):
    recipients = []
    printer_id = None
    for identifier in identifiers:
        row = db.execute(text('SELECT user_id,printer_id FROM alerts WHERE id=:id'), {'id': identifier}).first()
        if row:
            recipients.append(row[0])
            printer_id = row[1]
        db.execute(text('UPDATE alerts SET is_read=TRUE,is_dismissed=TRUE WHERE id=:id'), {'id': identifier})
    if recipients:
        event(db, 'restart_question_changed', printer_id, recipients)
