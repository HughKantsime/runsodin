"""Restartable content purge. Each item has a committed intent before byte removal."""
import json
import time
import uuid
from contextlib import nullcontext
from sqlalchemy.exc import SQLAlchemyError
from modules.organizations.education_term_files import prepare_upload, unlink_upload
from fastapi import HTTPException
from sqlalchemy import text
from modules.organizations.education_term_retention import (
    authority, policy, preview, submissions, blocked_reason, stamp, audit, content_revision, media_rows,
)



def _receipt(db, term_id, sid, kind, state):
    db.execute(text('INSERT INTO education_term_cleanup(term_id,submission_id,kind,state,updated_at) '
        'VALUES(:term,:sid,:kind,:state,:at) ON CONFLICT(term_id,submission_id,kind) '
        'DO UPDATE SET state=:state,updated_at=:at'),
        {'term': term_id, 'sid': sid, 'kind': kind, 'state': state, 'at': stamp()})


def _archive(db, row, principal):
    db.execute(text('UPDATE education_submissions SET archived_at=COALESCE(archived_at,CURRENT_TIMESTAMP),'
        'archived_by=COALESCE(archived_by,:actor),archive_reason=COALESCE(archive_reason,\'Term retention\'),'
        'lifecycle_revision=lifecycle_revision+1 WHERE id=:sid AND archived_at IS NULL'),
        {'sid': row['id'], 'actor': principal['id']})
    db.execute(text('UPDATE models SET hidden_from_library=TRUE WHERE id=:id'), {'id': row['model_id']})


def _files(db, row, prepared):
    # Keep upload identity/hash and the metadata graph. Release charged storage once.
    op = db.execute(text('SELECT accounted_bytes FROM education_upload_operations WHERE operation_id=:op'),
                    {'op': row['operation_id']}).scalar_one()
    if op:
        accounts = db.execute(text('SELECT scope_kind,scope_id,accounted_bytes FROM education_storage_accounts '
            "WHERE org_id=:org AND ((scope_kind='user' AND scope_id=:uid) OR (scope_kind='tenant' AND scope_id=:org))"),
            {'org': row['org_id'], 'uid': row['submitted_by']}).all()
        if len(accounts) != 2 or any(a.accounted_bytes < op for a in accounts):
            raise HTTPException(409, 'Storage accounting requires reconciliation')
    unlink_upload(row, prepared=prepared)
    db.execute(text('UPDATE education_storage_accounts SET accounted_bytes=accounted_bytes-:amount,revision=revision+1 '
        "WHERE org_id=:org AND ((scope_kind='user' AND scope_id=:uid) OR (scope_kind='tenant' AND scope_id=:org))"),
        {'amount': op, 'org': row['org_id'], 'uid': row['submitted_by']})
    db.execute(text('UPDATE education_upload_operations SET accounted_bytes=0,purge_requested_at=COALESCE(purge_requested_at,CURRENT_TIMESTAMP),'
        "purged_at=CURRENT_TIMESTAMP,state='purged' WHERE operation_id=:op"), {'op': row['operation_id']})
    db.execute(text("UPDATE print_files SET blob_state='purged',storage_bytes=0,mesh_data=NULL WHERE id=:id"),
               {'id': row['print_file_id']})


def _thumbnails(db, row):
    db.execute(text('UPDATE print_files SET thumbnail_b64=NULL WHERE id=:id'), {'id': row['print_file_id']})
    db.execute(text('UPDATE models SET thumbnail_b64=NULL,thumbnail_url=NULL WHERE id=:id'), {'id': row['model_id']})
    db.execute(text('UPDATE print_archives SET thumbnail_b64=NULL,plate_thumbnails=NULL WHERE education_submission_id=:id'), {'id': row['id']})



MAX_REQUEST_ITEMS = 25
MAX_REQUEST_SECONDS = 10


def current_submission(db, term, sid, *, lock=False):
    if lock and db.get_bind().dialect.name == 'postgresql':
        # Parent row locks fence new foreign-key references without locking entire tables.
        db.execute(text('SELECT id FROM education_submissions WHERE id=:sid FOR UPDATE'), {'sid': sid})
    rows = submissions(db, term, after_id=sid - 1, limit=1)
    if not rows or rows[0]['id'] != sid:
        raise HTTPException(404, 'submission_not_found')
    row = rows[0]
    if lock and db.get_bind().dialect.name == 'postgresql':
        for table, key, value in (
            ('jobs', 'id', row['job_id']), ('models', 'id', row['model_id']),
            ('print_files', 'id', row['print_file_id']),
            ('education_upload_operations', 'operation_id', row['operation_id']),
            ('print_archives', 'education_submission_id', sid),
        ):
            # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- table/key iterate the five literal parent-table pairs above; row identifiers use the key bind
            db.execute(text(f'SELECT {key} FROM {table} WHERE {key}=:key FOR UPDATE'), {'key': value}).all()
        db.execute(text("SELECT scope_kind,scope_id FROM education_storage_accounts WHERE org_id=:org "
                        "AND ((scope_kind='user' AND scope_id=:uid) OR (scope_kind='tenant' AND scope_id=:org)) "
                        "ORDER BY scope_kind,scope_id FOR UPDATE"), {'org': row['org_id'], 'uid': row['submitted_by']}).all()
        row = submissions(db, term, after_id=sid - 1, limit=1)[0]
    return row


def education_audit(db, row, kind, principal, term_id):
    event = str(uuid.uuid4())
    db.execute(text('INSERT INTO education_audit_events '
        '(event_id,org_id,actor_kind,actor_id,action,command_id,request_hash,resource_type,'
        'resource_id,cost_center_id,lifecycle_revision,details_json,result_json) '
        "VALUES(:event,:org,'user',:actor,:action,:event,:digest,'submission',:sid,:center,:revision,:details,:result)"),
        {'event': event, 'org': row['org_id'], 'actor': str(principal['id']),
         'action': 'term_retention_' + ('archived' if kind == 'portfolio' else 'purged'),
         'digest': content_revision(row), 'sid': str(row['id']), 'center': row['cost_center_id'],
         'revision': row['lifecycle_revision'], 'details': json.dumps({'term_id': term_id, 'kind': kind}),
         'result': json.dumps({'state': 'done'})})


def reason_code(error):
    if isinstance(error, SQLAlchemyError):
        return 'database_error'
    if isinstance(error, OSError):
        return 'filesystem_error'
    if isinstance(error, HTTPException):
        detail = str(error.detail)
        if detail == 'submission_not_found':
            return 'submission_gone'
        if detail.isidentifier() and detail.islower():
            return detail
        if 'Upload' in detail:
            return 'upload_ownership_or_hash_failed'
        if 'Storage' in detail:
            return 'storage_accounting_unconfirmed'
        if error.status_code == 403:
            return 'administrator_authority_changed'
    return 'cleanup_unconfirmed'


def remaining_kinds(db, term_id, sid, kinds):
    done = set(db.execute(text("SELECT kind FROM education_term_cleanup WHERE term_id=:term "
        "AND submission_id=:sid AND state='done'"), {'term': term_id, 'sid': sid}).scalars())
    return [kind for kind in kinds if kind not in done]


def record_blocked(db, term_id, sid, kinds, reason, results):
    try:
        # Serialize with purgers before reporting failures: a winner may have
        # completed these categories while this request hashed or rolled back.
        if db.get_bind().dialect.name == 'postgresql':
            db.execute(text("SET LOCAL lock_timeout = '1s'"))
            db.execute(text('SELECT id FROM education_term_policies WHERE id=:term FOR UPDATE'), {'term': term_id})
        else:
            connection = db.connection()
            if not connection.connection.driver_connection.in_transaction:
                connection.exec_driver_sql('BEGIN IMMEDIATE')
        kinds = remaining_kinds(db, term_id, sid, kinds)
        for kind in kinds:
            audit(db, 'item_blocked', term_id, {'submission_id': sid, 'kind': kind, 'reason': reason})
        db.commit()
    except SQLAlchemyError:
        db.rollback()  # A failed database cannot persist an audit; results still name the failure.
    results.extend({'submission_id': sid, 'kind': kind, 'state': 'blocked', 'reason': reason} for kind in kinds)



def finish_media(db, term, row, expected, item, principal, retire_media, started, results):
    sid, term_id, org = row['id'], term['id'], term['org_id']
    try:
        if retire_media is None:
            raise HTTPException(503, 'media_service_unavailable')
        for media in item['timelapses']:
            if time.monotonic() - started >= MAX_REQUEST_SECONDS:
                raise HTTPException(409, 'request_budget_exhausted')
            retire_media(media)
        authority(db, principal, org, lock=True)
        policy(db, term_id, org, lock=True)
        row = current_submission(db, term, sid, lock=True)
        refusal = blocked_reason(db, row)
        if refusal or content_revision(row) != expected:
            raise HTTPException(409, refusal or 'submission_changed')
        if media_rows(db, row):
            raise HTTPException(409, 'timelapse_cleanup_unconfirmed')
        _receipt(db, term_id, sid, 'timelapses', 'done')
        education_audit(db, row, 'timelapses', principal, term_id)
        audit(db, 'item_completed', term_id, {'submission_id': sid, 'kind': 'timelapses'})
        db.commit()
        results.append({'submission_id': sid, 'kind': 'timelapses', 'state': 'done'})
    except (HTTPException, OSError, ValueError, SQLAlchemyError) as error:
        db.rollback()
        reason = reason_code(error)
        record_blocked(db, term_id, sid, ['timelapses'], reason, results)
        return reason == 'request_budget_exhausted'
    return False


def purge(db, term_id, fingerprint, principal, org_id=None, *, retire_media=None, after_id=0):
    org = authority(db, principal, org_id)
    term = policy(db, term_id, org)
    plan = preview(db, term, after_id=after_id)
    if fingerprint != plan['fingerprint']:
        raise HTTPException(409, 'Dry run changed; preview again')
    audit(db, 'purge_started', term_id, {'org_id': org, 'fingerprint': fingerprint})
    db.commit()
    results, processed = [], 0
    started = time.monotonic()
    cursor = after_id
    for item in plan['items']:
        if processed >= MAX_REQUEST_ITEMS or time.monotonic() - started >= MAX_REQUEST_SECONDS:
            break
        sid, kinds = item['submission_id'], list(item['kinds'])
        if not kinds:
            cursor = sid
            continue
        processed += 1
        if item['blocked_reason']:
            record_blocked(db, term_id, sid, kinds, item['blocked_reason'], results)
            cursor = sid
            continue
        try:
            row = current_submission(db, term, sid)
            if content_revision(row) != item['content_revision']:
                raise HTTPException(409, 'submission_changed')
            pending = db.execute(text("SELECT state FROM education_term_cleanup WHERE term_id=:term AND submission_id=:sid AND kind='files'"),
                                 {'term': term_id, 'sid': sid}).scalar() == 'pending'
            db.rollback()  # Hashing never holds a DB transaction or writer lock.
            with prepare_upload(row, allow_absent=pending, deadline=started + MAX_REQUEST_SECONDS) if 'files' in kinds else nullcontext(None) as prepared:
                authority(db, principal, org, lock=True)
                policy(db, term_id, org, lock=True)
                kinds = remaining_kinds(db, term_id, sid, kinds)
                if not kinds:
                    db.rollback()
                    cursor = sid
                    continue
                row = current_submission(db, term, sid, lock=True)
                refusal = blocked_reason(db, row)
                if refusal:
                    raise HTTPException(409, refusal)
                if content_revision(row) != item['content_revision']:
                    raise HTTPException(409, 'submission_changed')
                for kind in kinds:
                    _receipt(db, term_id, sid, kind, 'pending')
                    audit(db, 'item_started', term_id, {'submission_id': sid, 'kind': kind})
                expected = content_revision(row)
                db.commit()
                media_block = item['blocked_kinds'].get('timelapses')
                defer_media = 'timelapses' in kinds and bool(item['timelapses'])
                authority(db, principal, org, lock=True)
                policy(db, term_id, org, lock=True)
                kinds = remaining_kinds(db, term_id, sid, kinds)
                if not kinds:
                    db.rollback()
                    cursor = sid
                    continue
                row = current_submission(db, term, sid, lock=True)
                refusal = blocked_reason(db, row)
                if refusal or content_revision(row) != expected:
                    raise HTTPException(409, refusal or 'submission_changed')
                if 'timelapses' in kinds and not defer_media and media_rows(db, row):
                    media_block = 'timelapse_cleanup_unconfirmed'
                for kind in kinds:
                    if kind == 'timelapses' and (media_block or defer_media):
                        continue
                    if kind == 'files':
                        _files(db, row, prepared)
                    elif kind == 'thumbnails':
                        _thumbnails(db, row)
                    elif kind == 'portfolio':
                        _archive(db, row, principal)
                        row = current_submission(db, term, sid)
                    _receipt(db, term_id, sid, kind, 'done')
                    education_audit(db, row, kind, principal, term_id)
                    audit(db, 'item_completed', term_id, {'submission_id': sid, 'kind': kind})
                expected_media = content_revision(current_submission(db, term, sid))
                db.commit()
                results.extend({'submission_id': sid, 'kind': k, 'state': 'done'} for k in kinds
                               if k != 'timelapses' or not (media_block or defer_media))
                if 'timelapses' in kinds and media_block:
                    record_blocked(db, term_id, sid, ['timelapses'], media_block, results)
                elif defer_media:
                    if finish_media(db, term, row, expected_media, item, principal, retire_media, started, results):
                        break  # Resume this submission's remaining videos, using its pending receipt.
        except (HTTPException, OSError, ValueError, SQLAlchemyError) as error:
            db.rollback()
            reason = reason_code(error)
            if reason == 'request_budget_exhausted':
                if processed > 1:
                    break  # Retry this item with the next request's full budget.
                reason = 'hash_budget_exhausted'
            record_blocked(db, term_id, sid, kinds, reason, results)
        cursor = sid
    remaining = any(i['submission_id'] > cursor for i in plan['items'])
    return {'results': results, 'blocked_submissions': [i for i in plan['items'] if i['blocked_reason']],
            'next_after_id': cursor if remaining else plan['next_after_id']}
