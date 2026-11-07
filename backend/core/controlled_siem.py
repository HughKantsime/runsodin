"""SIEM stays inside the customer's trusted boundary; canonical rows are not redacted."""
from sqlalchemy import text
from core.controlled_parts import refuse


def lock_policy(db):
    # Keep this lock before job/config locks. Using the audit-chain lock here
    # would invert ordinary job writers' job -> audit ordering on PostgreSQL.
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text -- fixed internal mutex key/value, no external input
    db.execute(text("INSERT INTO system_config(key,value) VALUES('controlled:siem_policy_lock','0') ON CONFLICT(key) DO NOTHING"))
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text -- fixed internal mutex key, no external input
    db.execute(text("UPDATE system_config SET value=value WHERE key='controlled:siem_policy_lock'"))


def marks_exist(db):
    # Bypass request caches after taking the shared controlled/config writer lock.
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text -- fixed table names, no interpolated SQL or input
    return db.execute(text('SELECT 1 FROM controlled_files UNION ALL SELECT 1 FROM controlled_jobs LIMIT 1')).first() is not None


def acknowledge_siem(db, actor, acknowledged):
    if acknowledged is not True:
        refuse(db, 'controlled_part_siem_acknowledgement_required', 'system', None)
    if not actor or actor.get('role') != 'admin' or actor.get('group_id') is not None:
        refuse(db, 'controlled_part_superadmin_required', 'system', None)


def first_mark(db, actor, acknowledged):
    if marks_exist(db):
        return
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text -- constant enabled-destination lookup without input
    if db.execute(text('SELECT 1 FROM audit_siem_destinations WHERE enabled=1 LIMIT 1')).first():
        acknowledge_siem(db, actor, acknowledged)
        return True
    return False
