"""Failure reporting over caller-visible jobs; retains the existing creation window."""
from sqlalchemy import or_, text
from core.base import JobStatus
from core.print_outcomes import JOB_DID_NOT_COMPLETE_SQL, job_outcome
from core.rbac import get_org_scope, should_filter_org, check_org_access
from modules.jobs.models import Job


def build_failure_analytics(db, current_user, cutoff):
    """Fleet failure analytics — rates by printer, model, filament, common reasons, HMS errors."""
    org = get_org_scope(current_user)

    # Completed and did-not-complete jobs in the existing creation window
    jobs_q = (
        db.query(Job)
        .filter(or_(Job.status == JobStatus.COMPLETED, text(JOB_DID_NOT_COMPLETE_SQL.format(prefix="jobs."))), Job.created_at >= cutoff)
    )
    from core.controlled_parts import job_visibility
    clause, params = job_visibility(current_user)
    jobs_q = jobs_q.filter(text(clause)).params(**params)
    if should_filter_org(current_user, org):
        jobs_q = jobs_q.filter((Job.charged_to_org_id == org) | (Job.charged_to_org_id == None))
    jobs = jobs_q.all()

    # --- By printer ---
    by_printer = {}
    for j in jobs:
        if not j.printer_id or not j.printer or not (check_org_access(current_user, j.printer.org_id) or j.printer.shared):
            continue
        p = by_printer.setdefault(j.printer_id, {"name": j.printer.name if j.printer else str(j.printer_id), "completed": 0, "failed": 0, "stopped": 0, "did_not_complete": 0, "fail_timestamps": []})
        if j.status.value == "completed":
            p["completed"] += 1
        else:
            p[job_outcome(j.status, j.actual_end)] += 1
            p["did_not_complete"] += 1
            if j.actual_end:
                p["fail_timestamps"].append(j.actual_end)

    printer_stats = []
    for pid, p in by_printer.items():
        total = p["completed"] + p["did_not_complete"]
        success_rate = round(p["completed"] / total * 100, 1) if total else 0
        # MTBF: average time between failures
        mtbf_hours = None
        if len(p["fail_timestamps"]) >= 2:
            ts = sorted(p["fail_timestamps"])
            gaps = [(ts[i+1] - ts[i]).total_seconds() / 3600 for i in range(len(ts)-1)]
            mtbf_hours = round(sum(gaps) / len(gaps), 1)
        printer_stats.append({
            "name": p["name"],
            "completed": p["completed"],
            "failed": p["failed"],
            "stopped": p["stopped"],
            "did_not_complete": p["did_not_complete"],
            "success_rate": success_rate,
            "mtbf_hours": mtbf_hours,
        })

    # --- By model ---
    by_model = {}
    for j in jobs:
        if j.model and not check_org_access(current_user, j.model.org_id):
            continue
        from core.controlled_parts import allowed
        name = (j.model.name if j.model else j.item_name) if allowed(db, "job", j.id, current_user) else "Controlled work"
        m = by_model.setdefault(name, {"completed": 0, "failed": 0, "stopped": 0, "did_not_complete": 0})
        if j.status.value == "completed":
            m["completed"] += 1
        else:
            m[job_outcome(j.status, j.actual_end)] += 1
            m["did_not_complete"] += 1

    model_stats = [
        {"name": k, "completed": v["completed"], "failed": v["failed"], "stopped": v["stopped"], "did_not_complete": v["did_not_complete"],
         "success_rate": round(v["completed"] / (v["completed"] + v["did_not_complete"]) * 100, 1)}
        for k, v in by_model.items() if v["completed"] + v["did_not_complete"] >= 2
    ]
    model_stats.sort(key=lambda x: x["success_rate"])

    # --- By filament type ---
    by_filament = {}
    for j in jobs:
        ft = j.filament_type.value if j.filament_type else "unknown"
        f = by_filament.setdefault(ft, {"completed": 0, "failed": 0, "stopped": 0, "did_not_complete": 0})
        if j.status.value == "completed":
            f["completed"] += 1
        else:
            f[job_outcome(j.status, j.actual_end)] += 1
            f["did_not_complete"] += 1

    filament_stats = [
        {"type": k, "completed": v["completed"], "failed": v["failed"], "stopped": v["stopped"], "did_not_complete": v["did_not_complete"],
         "failure_rate": round(v["did_not_complete"] / (v["completed"] + v["did_not_complete"]) * 100, 1)}
        for k, v in by_filament.items() if v["completed"] + v["did_not_complete"] >= 1
    ]

    # --- Failure reasons ---
    failed_jobs = [j for j in jobs if job_outcome(j.status, j.actual_end) == "failed"]
    reason_counts = {}
    for j in failed_jobs:
        r = j.fail_reason or "unspecified"
        reason_counts[r] = reason_counts.get(r, 0) + 1
    stopped_jobs = [j for j in jobs if job_outcome(j.status, j.actual_end) == "stopped"]
    for j in stopped_jobs:
        reason = j.fail_reason or "stopped"
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    top_reasons = sorted(reason_counts.items(), key=lambda x: -x[1])[:10]

    # --- HMS error frequency ---
    if should_filter_org(current_user, org):
        hms_rows = db.execute(
            text(
                "SELECT h.code, h.message, COUNT(*) as cnt FROM hms_error_history h "
                "JOIN printers p ON h.printer_id = p.id "
                "WHERE (h.source IS NULL OR h.source <> 'bambu_notice') AND h.occurred_at >= :cutoff AND (p.org_id = :org OR p.org_id IS NULL OR p.shared IS TRUE) "
                "GROUP BY h.code, h.message ORDER BY cnt DESC, h.code, h.message LIMIT 10"
            ),
            {"cutoff": cutoff.isoformat(), "org": org},
        ).fetchall()
    else:
        hms_rows = db.execute(
            text("SELECT code, message, COUNT(*) as cnt FROM hms_error_history WHERE (source IS NULL OR source <> 'bambu_notice') AND occurred_at >= :cutoff GROUP BY code, message ORDER BY cnt DESC, code, message LIMIT 10"),
            {"cutoff": cutoff.isoformat()},
        ).fetchall()
    hms_errors = [{"code": r[0], "message": r[1], "count": r[2]} for r in hms_rows]

    total_completed = sum(1 for j in jobs if j.status.value == "completed")
    total_failed = len(failed_jobs)
    total_stopped = len(stopped_jobs)
    total_did_not_complete = total_failed + total_stopped

    return {
        "total_completed": total_completed,
        "total_failed": total_failed,
        "total_stopped": total_stopped,
        "total_did_not_complete": total_did_not_complete,
        "overall_success_rate": round(total_completed / (total_completed + total_did_not_complete) * 100, 1) if (total_completed + total_did_not_complete) else 0,
        "by_printer": printer_stats,
        "by_model": model_stats[:15],
        "by_filament": filament_stats,
        "top_failure_reasons": [{"reason": r, "count": c} for r, c in top_reasons],
        "hms_errors": hms_errors,
    }

