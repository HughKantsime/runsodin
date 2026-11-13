"""Cost per print by model or product: actual cost of completed jobs next to their estimates.

Pro (analytics). Built from modules.jobs.cost_actual, so the same honesty rules apply: only jobs
whose actual cost is complete enter an average, a group needs MINIMUM_JOBS of them before any
average is shown, and nothing here is a profit or margin figure.
"""
import csv
import io
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import bindparam, func, text
from sqlalchemy.orm import Session

from core.audit import audit_download, export_filters
from core.base import JobStatus
from core.db import get_db
from core.rbac import check_org_access, get_org_scope, require_role, should_filter_org
from license_manager import feature_gate, require_feature
from modules.jobs.models import Job
from modules.models_library.models import Model

router = APIRouter(tags=["Analytics"])

MINIMUM_JOBS = 3
_NOT_EDUCATION = "NOT EXISTS (SELECT 1 FROM education_submissions es WHERE es.job_id=jobs.id)"


def _window(db, start: Optional[date], end: Optional[date]):
    """[from 00:00, day after `to` 00:00) in the farm's time zone, as naive UTC."""
    from core.farm_time import farm_zone
    zone = farm_zone(db)

    def utc(day):
        return datetime.combine(day, time.min, tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)
    return (utc(start) if start else None), (utc(end + timedelta(days=1)) if end else None)


def _completed_jobs(db, user, start, end):
    from core.controlled_parts import job_visibility
    clause, params = job_visibility(user)
    query = (db.query(Job).filter(Job.status == JobStatus.COMPLETED)
             .filter(text(clause)).params(**params).filter(text(_NOT_EDUCATION)))
    org = get_org_scope(user)
    if should_filter_org(user, org):
        query = query.filter((Job.charged_to_org_id == org) | (Job.charged_to_org_id == None))  # noqa: E711
    # Same window rule as the other analytics: the day a job ended, or was created when no end is recorded.
    ended = func.coalesce(Job.actual_end, Job.created_at)
    if start is not None:
        query = query.filter(ended >= start)
    if end is not None:
        query = query.filter(ended < end)
    return query.order_by(Job.id).all()


def _model_names(db, user, jobs):
    ids = sorted({job.model_id for job in jobs if job.model_id is not None})
    if not ids:
        return {}
    from core.controlled_parts import model_visibility
    clause, params = model_visibility(user)
    rows = db.query(Model.id, Model.name, Model.org_id).filter(Model.id.in_(ids)).filter(text(clause)).params(**params).all()
    return {row.id: row.name for row in rows if check_org_access(user, row.org_id)}


def _product_names(db, user, jobs):
    """({job_id: product_id}, {product_id: name}) through the job's order line."""
    items = sorted({job.order_item_id for job in jobs if job.order_item_id is not None})
    if not items:
        return {}, {}
    rows = db.execute(text("SELECT i.id AS item_id,p.id AS product_id,p.name,p.org_id FROM order_items i "
                           "JOIN products p ON p.id=i.product_id WHERE i.id IN :ids").bindparams(bindparam("ids", expanding=True)),
                      {"ids": items}).mappings().all()
    visible = {row["item_id"]: row for row in rows if check_org_access(user, row["org_id"])}
    by_job = {job.id: visible[job.order_item_id]["product_id"] for job in jobs if job.order_item_id in visible}
    return by_job, {row["product_id"]: row["name"] for row in visible.values()}


def _pieces(job):
    return job.quantity_on_bed if type(job.quantity_on_bed) is int and job.quantity_on_bed >= 1 else None


def summarize(name, jobs, costs):
    """One table row for a group of completed jobs. Averages only from complete actual costs."""
    known = [(job, costs[job.id]) for job in jobs if costs[job.id]["state"] == "complete" and _pieces(job)]
    row = {"name": name, "jobs": len(jobs), "jobs_with_full_cost": len(known),
           "jobs_partial": sum(costs[job.id]["state"] == "partial" for job in jobs),
           "jobs_without_usage": sum(costs[job.id]["state"] == "none" for job in jobs),
           "pieces": sum(_pieces(job) for job, _ in known),
           "average_actual_per_piece": None, "average_estimate_per_piece": None,
           "variance_percent": None, "failure_share_percent": None}
    if len(known) < MINIMUM_JOBS:
        return row
    actual = sum(result["_exact"]["total"] for _, result in known)
    row["average_actual_per_piece"] = round(actual / row["pieces"], 2)
    if actual > 0:
        row["failure_share_percent"] = round(sum(result["_exact"]["failed"] for _, result in known) / actual * 100, 1)
    # Estimate and variance only over the jobs that have both figures, so like is compared with like.
    both = [(job, result) for job, result in known
            if isinstance(job.estimated_cost, (int, float)) and job.estimated_cost >= 0]
    if len(both) >= MINIMUM_JOBS:
        estimate = sum(job.estimated_cost for job, _ in both)
        row["average_estimate_per_piece"] = round(estimate / sum(_pieces(job) for job, _ in both), 2)
        if estimate > 0:
            row["variance_percent"] = round((sum(result["_exact"]["total"] for _, result in both) - estimate) / estimate * 100, 1)
    return row


def build(db, user, start: Optional[date], end: Optional[date], group: str):
    from modules.models_library.services import saved_pricing_config
    from modules.jobs.services import job_actual_costs
    config, configured = saved_pricing_config(db)
    body = {"configured": configured, "group": group, "from": start.isoformat() if start else None,
            "to": end.isoformat() if end else None, "minimum_jobs": MINIMUM_JOBS, "rows": []}
    if not configured:
        return body
    jobs = _completed_jobs(db, user, *_window(db, start, end))
    costs = job_actual_costs(db, jobs, config)
    if group == "product":
        key_of, names = _product_names(db, user, jobs)
    else:
        names = _model_names(db, user, jobs)
        key_of = {job.id: job.model_id for job in jobs if job.model_id in names}
    groups = {}
    for job in jobs:
        groups.setdefault(key_of.get(job.id), []).append(job)
    rows = [dict(id=key, **summarize(names.get(key), members, costs)) for key, members in groups.items()]
    body["rows"] = sorted(rows, key=lambda row: (row["id"] is None, (row["name"] or "").lower(), row["id"] or 0))
    return body


@router.get("/analytics/cost-per-print", dependencies=[Depends(feature_gate("analytics"))])
def get_cost_per_print(
    start: Optional[date] = Query(default=None, alias="from"),
    end: Optional[date] = Query(default=None, alias="to"),
    group: Literal["model", "product"] = "model",
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role("viewer", scope="read:analytics")),
):
    """Average actual cost per piece of completed jobs, by model or by product.

    from / to are calendar days in the farm's time zone, both included. A row's averages are null
    until it has `minimum_jobs` completed jobs whose actual cost is complete. variance_percent is
    (actual - estimate) / estimate: positive means the prints cost more than estimated.
    failure_share_percent is the part of the actual cost spent on failed and cancelled attempts.
    The row with id null holds jobs with no model (or no product) the caller may see. Education
    submissions are left out; controlled work follows the caller's audience.
    """
    return build(db, current_user, start, end, group)


@router.get("/export/cost-per-print", dependencies=[Depends(feature_gate("analytics"))])
def export_cost_per_print(
    start: Optional[date] = Query(default=None, alias="from"),
    end: Optional[date] = Query(default=None, alias="to"),
    group: Literal["model", "product"] = "model",
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role("operator", scope="read:analytics")),
):
    """The cost per print table as CSV."""
    require_feature("csv_export")
    body = build(db, current_user, start, end, group)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Model" if group == "model" else "Product", "Completed jobs", "Jobs with full cost records",
                     "Pieces made", "Average actual cost per piece", "Average estimate per piece",
                     "Actual vs estimate (%)", "Spent on failed attempts (%)"])
    for row in body["rows"]:
        name = row["name"] or ("No model" if group == "model" else "No product")
        writer.writerow(["'" + name if name.startswith(("=", "+", "-", "@", "\t", "\r")) else name,
                         row["jobs"], row["jobs_with_full_cost"], row["pieces"],
                         *("" if row[key] is None else row[key] for key in
                           ("average_actual_per_piece", "average_estimate_per_piece", "variance_percent", "failure_share_percent"))])
    audit_download(db, "export.downloaded", details={"export": "cost_per_print",
                                                     "filters": export_filters(start=start, end=end, group=group)})
    output.seek(0)
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=cost_per_print.csv"})
