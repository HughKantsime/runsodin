"""Named refusals before deleting directly marked jobs."""
from sqlalchemy import text, bindparam
from core.controlled_parts import refuse


def assert_jobs_deletable(db, identifiers):
    query = text('SELECT job_id FROM controlled_jobs WHERE job_id IN :ids LIMIT 1').bindparams(bindparam('ids', expanding=True))
    identifier = db.execute(query, {'ids': list(identifiers)}).scalar()
    if identifier is not None:
        refuse(db, 'controlled_part_release_required', 'job', identifier)


def controlled_job_ids(db, identifiers):
    """Snapshot policies before unlinking the source, in the deletion transaction."""
    from core.controlled_parts import policies
    identifiers = sorted(set(identifiers))
    # Allocate the deletion audit sequence only after prior job writers finish.
    if identifiers and db.get_bind().dialect.name == 'postgresql':
        query = text('SELECT id FROM jobs WHERE id IN :ids ORDER BY id FOR UPDATE').bindparams(bindparam('ids', expanding=True))
        db.execute(query, {'ids': identifiers}).all()
    captured = {}
    for identifier in identifiers:
        rules = policies(db, 'job', identifier)
        if rules:
            captured[identifier] = rules
    return captured


def redact_job_history_ids(db, captured):
    """Persist audiences; audit bytes are immutable, including for entitled readers."""
    from datetime import datetime, timezone
    from core.audit import log_audit
    for identifier, rules in captured.items():
        log_audit(db, 'controlled_part.job_deleted', 'job', identifier)
        db.flush()
        # Read the bound only after the job-row lock and deletion audit write.
        audit_max = db.execute(text('SELECT COALESCE(max(id),0) FROM audit_logs')).scalar_one()
        now = datetime.now(timezone.utc).isoformat()
        # Preserve every source's clock, including multiple rules for one audience.
        for rule_index, rule in enumerate(rules):
            db.execute(text("""INSERT INTO controlled_job_tombstones
                (job_id,audience_id,label,printer_tag,retention_days,marked_at,created_at,audit_max_id,
                 purge_previewed_at,retention_initial_days,retention_changes,rule_index)
                VALUES (:job,:audience_id,:label,:printer_tag,:retention_days,:marked_at,:created,:audit_max,
                        :purge_previewed_at,:retention_initial_days,:retention_changes,:rule_index)"""),
                {**rule, 'job': identifier, 'created': now, 'audit_max': audit_max,
                 'rule_index': rule_index})


def redact_deleted_job_history(db, identifier):
    redact_job_history_ids(db, controlled_job_ids(db, [identifier]))
