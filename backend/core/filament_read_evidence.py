"""Batch-only read evidence for archive causes; deduction writers use live reads."""
from dataclasses import dataclass
from sqlalchemy import bindparam, text


@dataclass
class FilamentReadEvidence:
    config: dict
    prints: dict
    jobs: dict
    resets: dict
    usage: list


def batch_runner(db, print_ids):
    from core.print_attempts import sqlalchemy_runner
    run = sqlalchemy_runner(db)
    if not print_ids:
        return run
    def rows(sql, ids):
        return db.execute(text(sql).bindparams(bindparam('ids', expanding=True)), {'ids': list(ids)}).mappings().all() if ids else []
    prints = rows('SELECT * FROM print_jobs WHERE id IN :ids', print_ids)
    job_ids = {p['scheduled_job_id'] for p in prints if p['scheduled_job_id'] is not None}
    # Include siblings for legacy rows attributed to another physical print.
    prints += rows('SELECT * FROM print_jobs WHERE scheduled_job_id IN :ids', job_ids)
    config = dict(db.execute(text("SELECT key,value FROM system_config WHERE key LIKE 'quota_attempt:%' OR key LIKE 'filament_settlement:print:%'")).all())
    resets = {}
    for row in rows("SELECT entity_id,timestamp FROM audit_logs WHERE action='job.reset' AND entity_type='job' AND entity_id IN :ids", job_ids):
        resets.setdefault(int(row['entity_id']), []).append(row['timestamp'])
    usage = rows('SELECT * FROM spool_usage WHERE job_id IN :ids', job_ids)
    seen = {r['id'] for r in usage}
    usage += [r for r in rows('SELECT * FROM spool_usage WHERE print_job_id IN :ids', print_ids) if r['id'] not in seen]
    run.filament_evidence = FilamentReadEvidence(config, {p['id']: p for p in prints},
        {j['id']: j for j in rows('SELECT id,actual_start FROM jobs WHERE id IN :ids', job_ids)}, resets, usage)
    return run
