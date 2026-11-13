"""Why a queued job is not printing (JobResponse.waiting_reason). Computed on read, never stored.

One reason per job, the first that applies:

    awaiting_approval       status 'submitted'
    on_hold                 the job's hold flag
    needs_filament_mapping  the last dispatch was refused for a missing AMS mapping and none is saved yet
    printer_offline         scheduled; the assigned printer is not reporting
    printer_busy            scheduled; the assigned printer is printing or paused (until: the print's estimated end)
    dispatch_refused        the last dispatch attempt was refused (detail: the refusal in plain words)
    quiet_hours             scheduled to start after the quiet hours that are in force now (until: their end)
    no_printer_can_run_it   pending; no active printer the job may use passes its pin, target, tags and material
                            (detail: the missing tag or material, from the scheduler's own material plan)

Printing and finished jobs have none. Reasons are computed only for the jobs the caller was already
given, in a fixed number of reads however many jobs there are. A printer the caller may not read is
never identified: its reason carries no printer_id, no end time and no detail. Printer names are
never included. The printers considered for no_printer_can_run_it are a superset of the scheduler's
(it also drops claimed, held, unsupported and unlicensed printers), so "no printer" stays true.
"""
from datetime import datetime, timedelta, timezone

from core.printer_display_facts import may_read
from core.rbac import check_org_access

QUEUED = ("pending", "scheduled")
MAPPING_REFUSAL = "AMS_MAPPING_REQUIRED"


def _status(job):
    return getattr(job.status, "value", job.status)


def _utc(value):
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def quiet_hours_end(db, now):
    """When the quiet hours in force at ``now`` end, or None outside them (the scheduler's own window rule)."""
    from core.scheduler_settings import blackout_is_off, get_blackout, get_blackout_timezone
    from modules.jobs.scheduler import Scheduler, SchedulerConfig
    start, end = get_blackout(db)
    if blackout_is_off(start, end):
        return None
    zone_name = get_blackout_timezone(db)
    try:
        config = SchedulerConfig.from_time_strings(blackout_start=start, blackout_end=end, blackout_timezone=zone_name)
    except (TypeError, ValueError):
        return None
    if not Scheduler(config)._is_blackout_time(now):
        return None
    # The window's clock times are in its saved zone (UTC when it has none): find the end on
    # that zone's clock, then answer in the same naive-UTC form as ``now``.
    from datetime import timezone as _tz
    zone = _tz.utc
    if zone_name:
        try:
            from zoneinfo import ZoneInfo
            zone = ZoneInfo(zone_name)
        except Exception:
            zone = _tz.utc          # same fallback as the scheduler
    aware = now if now.tzinfo is not None else now.replace(tzinfo=_tz.utc)
    local = aware.astimezone(zone)
    until = local.replace(hour=config.blackout_end_hour, minute=config.blackout_end_minute, second=0, microsecond=0)
    if until <= local:
        until = until + timedelta(days=1)
    until = until.astimezone(_tz.utc)
    return until if now.tzinfo is not None else until.replace(tzinfo=None)


def _tag_text(tags, printers, pinned):
    quoted = lambda names: " and ".join(f'"{name}"' for name in names)
    word = "tag" if len(tags) == 1 else "tags"
    if pinned:
        missing = [t for t in tags if t not in (printers[0].tags or [])]
        return f"the pinned printer does not have the {'tag' if len(missing) == 1 else 'tags'} {quoted(missing)}"
    nowhere = [t for t in tags if not any(t in (p.tags or []) for p in printers)]
    if nowhere:
        return f"no printer has the {'tag' if len(nowhere) == 1 else 'tags'} {quoted(nowhere)}"
    return f"no printer has the {word} {quoted(tags)} together"


def _no_printer(job, printers, authorities, plan, visible):
    """The reason no active printer can take this pending job, or None when one can (or it is unknown)."""
    from core.dispatch_identity import ownership_refusal
    from modules.jobs.pinning import job_pin
    from modules.jobs.scheduler import _printer_identity, _same_printer_model
    owner = {"charged_to_org_id": job.charged_to_org_id}
    usable = [p for p in printers if authorities[p.id] is not None and not ownership_refusal(owner, authorities[p.id])]
    pin = job_pin(job)
    if pin is not None:
        usable = [p for p in usable if p.id == pin]
        if not usable:
            return None  # an unusable pin is already reported as pinned_printer_issue
    target_type, target = (job.target_type or "specific"), job.target_filter
    if target_type == "model" and target:
        usable = [p for p in usable if _printer_identity(p) and _same_printer_model(_printer_identity(p), target)]
    elif target_type == "protocol" and target:
        usable = [p for p in usable if (p.api_type or "").lower() == str(target).lower()]
    if not usable:
        return {"code": "no_printer_can_run_it"}
    # The detail describes these printers: only for a caller who may read every one of them.
    describe = all(visible(p) for p in usable)
    tags = [t for t in (job.required_tags or []) if isinstance(t, str)]
    tagged = [p for p in usable if all(t in (p.tags or []) for t in tags)]
    if not tagged:
        return {"code": "no_printer_can_run_it", **({"detail": _tag_text(tags, usable, pin is not None)} if describe else {})}
    for printer in tagged:
        plan.excludes(job.id, printer.id)
    material = plan.reason(job.id, "pinned to this job" if pin is not None else None)
    if material is None:
        return None
    return {"code": "no_printer_can_run_it", **({"detail": material} if describe else {})}


def may_lack_a_printer(job):
    """Pending and not held: the only jobs no_printer_can_run_it is computed for."""
    return _status(job) == "pending" and job.hold is not True


def add_waiting_reasons(db, jobs, actor, now=None, active_printers=None):
    """Set job.waiting_reason on each already-visible job (None when it is not waiting on anything known).

    ``active_printers``: every active printer, when the caller has already read them.
    """
    now = now or datetime.now(timezone.utc)
    undecided = []
    for job in jobs:
        status = _status(job)
        job.waiting_reason = None
        if status == "submitted":
            job.waiting_reason = {"code": "awaiting_approval"}
        elif status in QUEUED and job.hold is True:
            job.waiting_reason = {"code": "on_hold"}
        elif status in QUEUED:
            undecided.append(job)
    if not undecided:
        return jobs

    def visible(printer):
        return may_read(actor, "printers") and (check_org_access(actor, printer.org_id) or bool(printer.shared))

    from modules.printers.services import latest_gate_refusals, printer_wait_states
    refusals = latest_gate_refusals(db, [job.id for job in undecided])
    assigned = {job.printer.id: job.printer for job in undecided
                if _status(job) == "scheduled" and job.printer_id is not None and job.printer is not None}
    states = printer_wait_states(db, list(assigned.values()), now) if assigned else {}
    quiet, pending = [], []
    for job in undecided:
        refused = refusals.get(job.id)
        scheduled = _status(job) == "scheduled"
        state = states.get(job.printer_id) if scheduled else None
        if refused and refused[0] == MAPPING_REFUSAL and job.ams_mapping is None:
            job.waiting_reason = {"code": "needs_filament_mapping"}
        elif state:
            reason = {"code": "printer_offline" if state[0] == "offline" else "printer_busy"}
            if visible(assigned[job.printer_id]):
                reason["printer_id"] = job.printer_id
                if state[1] is not None:
                    reason["until"] = state[1]
            job.waiting_reason = reason
        elif refused and refused[0] != MAPPING_REFUSAL:
            job.waiting_reason = {"code": "dispatch_refused", "detail": refused[1]}
        elif scheduled:
            quiet.append(job)
        else:
            pending.append(job)

    if any(job.scheduled_start is not None for job in quiet):
        until = quiet_hours_end(db, now)
        for job in quiet:
            if until is not None and job.scheduled_start is not None and _utc(job.scheduled_start) >= until:
                job.waiting_reason = {"code": "quiet_hours", "until": until}

    if pending:
        from sqlalchemy.orm import object_session
        from modules.jobs.pinning import BLOCKING, cached_job_issue
        from modules.jobs.scheduler import _printer_authority
        from modules.jobs.scheduler_materials import MaterialPlan
        from modules.printers.models import Printer
        printers = active_printers
        if printers is None:
            printers = db.query(Printer).filter(Printer.is_active.is_(True)).all()
        authorities = {p.id: _printer_authority(p.org_id, p.shared, p.is_active) for p in printers}
        plan = MaterialPlan.load(db, [p.id for p in printers])
        for job in pending:
            if cached_job_issue(object_session(job), job) in BLOCKING:
                continue
            job.waiting_reason = _no_printer(job, printers, authorities, plan, visible)
    return jobs
