"""Synchronous, restartable GDPR erasure shared by individual and term routes."""
import json
import logging
from fastapi import HTTPException
from sqlalchemy import inspect, text
from core.dependencies import log_audit
from modules.organizations.erasure_audit import erase_password_login_history, erase_sso_login_history
from modules.organizations.routes_users import _is_superadmin, _check_org_admin_access

log = logging.getLogger('odin.api')


def erase_user_data_sync(user_id, current_user, db):
    """Anonymize user data (GDPR Article 17). Admin only. Preserves job records for analytics."""
    user = db.execute(text("SELECT * FROM users WHERE id = :id"), {"id": user_id}).fetchone()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    # Org-scoped admin: verify target is in their group
    if not _is_superadmin(current_user):
        if not _check_org_admin_access(current_user, dict(user._mapping).get("group_id")):
            raise HTTPException(status_code=403, detail="Cannot erase users outside your group")
    if user.role == "admin":
        admin_count = db.execute(text("SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active IS TRUE")).scalar()
        if admin_count <= 1:
            raise HTTPException(status_code=400, detail="Cannot erase the last admin account")

    from modules.organizations.education_term_privacy import erase_term_identity
    erase_term_identity(db, user_id)
    user_data = dict(user._mapping)
    # Persist the source identity before the first account/chunk commit. A retry
    # must scrub the original values even after the account is a tombstone.
    recovery = db.execute(text('SELECT identifiers_json FROM education_erasure_pending WHERE user_id=:uid'),
                          {'uid': user_id}).scalar()
    if recovery:
        user_data.update(json.loads(recovery))
    else:
        saved = {key: user_data.get(key) for key in ('username', 'email', 'display_name')}
        db.execute(text('INSERT INTO education_erasure_pending(user_id,identifiers_json) VALUES(:uid,:identity)'),
                   {'uid': user_id, 'identity': json.dumps(saved)})

    identifiers = {
        value
        for value in (
            user_data.get("username"),
            user_data.get("email"),
            user_data.get("display_name"),
        )
        if isinstance(value, str) and value and value != "[deleted]"
    }

    db.execute(text("""UPDATE users SET
        username = :anon_name, email = '[deleted]', password_hash = '[deleted]',
        is_active = FALSE, mfa_enabled = FALSE, mfa_secret = NULL,
        oidc_subject = NULL, oidc_provider = NULL, oidc_issuer = NULL
        WHERE id = :id"""),
        {"anon_name": f"[deleted-{user_id}]", "id": user_id})

    db.execute(text("UPDATE job_timeline_events SET actor_user_id=NULL,actor_token_id=NULL WHERE actor_user_id=:uid OR actor_token_id IN (SELECT id FROM api_tokens WHERE user_id=:uid)"), {"uid": user_id})

    from core.audit_actor import erase_actor_history, actor_is_erased
    erase_actor_history(db, user_id)
    erase_sso_login_history(db, user_id)

    from core.privacy_replay import erase_identifying_replay
    try:
        erase_identifying_replay(db, user_id=user_id, identifiers=identifiers)
    except Exception:
        try:db.rollback()
        except Exception:pass
        raise HTTPException(status_code=503, detail="Erasure could not be confirmed. Try again or contact your administrator.") from None

    db.execute(text("DELETE FROM mfa_enrollments WHERE user_id = :uid"), {"uid": user_id})
    db.execute(text("DELETE FROM active_sessions WHERE user_id = :uid"), {"uid": user_id})
    db.execute(text("DELETE FROM api_tokens WHERE user_id = :uid"), {"uid": user_id})
    db.execute(text("DELETE FROM alert_preferences WHERE user_id = :uid"), {"uid": user_id})
    db.execute(text("DELETE FROM push_subscriptions WHERE user_id = :uid"), {"uid": user_id})
    db.execute(text("DELETE FROM push_devices WHERE user_id = :uid"), {"uid": user_id})
    former_username = user_data.get("username")
    if isinstance(former_username, str) and former_username:
        erase_password_login_history(db, former_username)

    # Submission/resubmission alerts are delivered to reviewers, so deleting
    # only alerts owned by this user would leave their former username or
    # email in other users' inboxes. Scrub the known account identifiers while
    # retaining the operational alert and job reference.
    from core.identity_scrub import like_patterns, scrub_text, scrub_json_text

    like = "ILIKE" if db.get_bind().dialect.name == "postgresql" else "LIKE"

    def _candidate_ids(table, column_expr):
        # Case-insensitive on both dialects (PostgreSQL LIKE is case-sensitive).
        ids = set()
        for identifier in identifiers:
            for pattern in like_patterns(identifier):
                ids.update(r.id for r in db.execute(
                    text(f"SELECT id FROM {table} WHERE {column_expr} {like} :p"), {"p": pattern}))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — table/column are module constants, no user input
        return sorted(ids)

    # Quiet-hours deferred deliveries (migration 032): the user's own copies go here;
    # copies of the same alert held for other recipients and webhooks are kept for
    # delivery and scrubbed structurally with the alerts below.
    from modules.organizations.routes_users import delete_deferred_deliveries, _DEFERRED_DELIVERIES_TABLE
    delete_deferred_deliveries(db, user_id=user_id)
    deferred_present = inspect(db.connection()).has_table(_DEFERRED_DELIVERIES_TABLE)

    # Remove direct Classroom identity data and any unconsumed OAuth state.
    # Operational course mappings and cost centers remain, while grants are
    # revoked so the tombstoned user cannot retain classroom access.
    db.execute(
        text(
            "UPDATE education_cost_center_grants SET state='revoked', revoked_by=:actor, "
            "revoked_at=CURRENT_TIMESTAMP WHERE user_id=:uid AND state='active'"
        ),
        {"actor": current_user["id"], "uid": user_id},
    )
    db.execute(text("DELETE FROM classroom_roster_identities WHERE user_id=:uid"), {"uid": user_id})
    db.execute(text("DELETE FROM classroom_oauth_states WHERE admin_id=:uid"), {"uid": user_id})
    db.execute(
        text("UPDATE classroom_connections SET created_by=NULL WHERE created_by=:uid"),
        {"uid": user_id},
    )
    former_email = user_data.get("email")
    if isinstance(former_email, str) and former_email:
        db.execute(
            text(
                "UPDATE classroom_connections SET account_subject=NULL, account_email=NULL, "
                "granted_scopes=NULL, refresh_token_encrypted=NULL, access_token_encrypted=NULL, "
                "access_token_expires_at=NULL, state='not_connected', "
                "last_error_code=NULL, updated_at=CURRENT_TIMESTAMP "
                "WHERE LOWER(account_email)=:email"
            ),
            {"email": former_email.lower()},
        )

    # Historical audit detail strings can contain the former username (for
    # example the export audit). Preserve event metadata while removing the
    # erased identity before writing the final erasure record.
    db.execute(
        text("UPDATE audit_logs SET details = :details WHERE entity_type = 'user' AND entity_id = :uid"),
        {"details": json.dumps({"anonymized_user_id": user_id}), "uid": user_id},
    )

    erasure_details = {"anonymized_user_id": user_id}
    if not actor_is_erased(db, current_user["id"]):
        erasure_details["actor_user_id"] = current_user["id"]
    log_audit(db, "gdpr_erasure", "user", user_id, erasure_details)
    db.commit()

    # Account anonymization is committed. Scrub the user's identifiers from the
    # free-text audit and alert tables in committed chunks: each chunk holds the
    # write lock only briefly, and a re-run after an interruption resumes from
    # whatever rows still match (idempotent).
    # Chunk the candidate rows so a large table is not rewritten in one
    # statement that holds the write lock for many seconds; log progress.
    CHUNK = 500
    alert_ids = _candidate_ids("alerts", "COALESCE(title,'') || ' ' || COALESCE(message,'')")
    for start in range(0, len(alert_ids), CHUNK):
        for alert_id in alert_ids[start:start + CHUNK]:
            row = db.execute(text("SELECT title, message FROM alerts WHERE id = :id"), {"id": alert_id}).one()
            title, message = scrub_text(row.title, identifiers), scrub_text(row.message, identifiers)
            if (title, message) != (row.title, row.message):
                db.execute(text("UPDATE alerts SET title = :t, message = :m WHERE id = :id"),
                           {"t": title, "m": message, "id": alert_id})
        db.commit()  # commit per chunk: bounded lock, idempotent and resumable
        if alert_ids:
            log.info("GDPR erase: scrubbed alerts %d/%d", min(start + CHUNK, len(alert_ids)), len(alert_ids))

    # The same structural scrub for the deferred copies (title/message), when 032 is applied.
    deferred_ids = _candidate_ids(_DEFERRED_DELIVERIES_TABLE, "COALESCE(title,'') || ' ' || COALESCE(message,'')") if deferred_present else []
    for start in range(0, len(deferred_ids), CHUNK):
        for deferred_id in deferred_ids[start:start + CHUNK]:
            row = db.execute(text(f"SELECT title, message FROM {_DEFERRED_DELIVERIES_TABLE} WHERE id = :id"),  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant table name; params bound
                             {"id": deferred_id}).one()
            title, message = scrub_text(row.title, identifiers), scrub_text(row.message, identifiers)
            if (title, message) != (row.title, row.message):
                db.execute(text(f"UPDATE {_DEFERRED_DELIVERIES_TABLE} SET title = :t, message = :m WHERE id = :id"),  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant table name; params bound
                           {"t": title, "m": message, "id": deferred_id})
        db.commit()  # commit per chunk: bounded lock, idempotent and resumable
        if deferred_ids:
            log.info("GDPR erase: scrubbed deferred deliveries %d/%d", min(start + CHUNK, len(deferred_ids)), len(deferred_ids))

    # Any other audit row can still name the user in its details (failed
    # logins have no entity id; actions the user performed carry their name).
    # Decode each candidate's JSON text (read as text on both dialects) and
    # scrub it structurally; rows that do not name the user stay byte-identical.
    audit_ids = _candidate_ids("audit_logs", "CAST(details AS TEXT)")
    for start in range(0, len(audit_ids), CHUNK):
        for audit_id in audit_ids[start:start + CHUNK]:
            # Password history was handled by exact identity above. A broad
            # free-text scrub could otherwise change a different login name.
            raw = db.execute(text("SELECT CAST(details AS TEXT) FROM audit_logs WHERE id = :id "
                                  "AND NOT (COALESCE(entity_type,'')='user' AND action IN "
                                  "('login_success','login_failed','auth.login_failed'))"), {"id": audit_id}).scalar()
            scrubbed = scrub_json_text(raw, identifiers)
            if scrubbed is not None:
                db.execute(text("UPDATE audit_logs SET details = :details WHERE id = :id"),
                           {"details": scrubbed, "id": audit_id})
        db.commit()  # commit per chunk: bounded lock, idempotent and resumable
        if audit_ids:
            log.info("GDPR erase: scrubbed audit_logs %d/%d", min(start + CHUNK, len(audit_ids)), len(audit_ids))

    db.execute(text('DELETE FROM education_erasure_pending WHERE user_id=:uid'), {'uid': user_id})
    db.commit()
    return {"status": "ok", "message": "User data erased"}
