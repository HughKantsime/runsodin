"""What a finished job cost to print, from recorded facts (spec: cost-per-print).

Computed on read; nothing is stored. Three kinds of numbers, never mixed without a label:

  material       recorded: grams each print attempt used from each spool x that spool's price
                 per gram as it was written down when the attempt started (failed and cancelled
                 attempts count)
  machine        recorded print hours of every attempt x today's electricity and machine rates
  from settings  labour, packaging, other costs and overhead, counted once per job exactly as
                 the job's estimate counts them (models_library.pricing_rule, one plate)

The failure allowance of the estimate is not added: real failures are already in the total
through their attempts. A fact that was not recorded stays unknown and makes the total partial
("at least"); it is never filled in from current prices. Where an attempt fed several spools
the per-spool grams come from the sliced file (the printer reports one total); those lines
count, and are labelled "from the sliced file" in the breakdown.

This is not an order ledger and not a profit figure (measured-order-costs.md owns those).
"""
from __future__ import annotations

import json
import math

from sqlalchemy import bindparam, text

FINISHED_JOB_STATUSES = ("completed", "failed", "cancelled")
ATTEMPT_OUTCOMES = ("completed", "failed", "cancelled")
# print_archives.accounting_state values whose consumption_json lists what each spool gave.
_ACCOUNTED = ("accounted", "reconciliation_required")
_CHUNK = 500


def _amount(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _money(value):
    return None if value is None else round(value, 2)


def cost_actual(attempts, config):
    """Actual cost of one job from its attempts and the saved pricing settings. Pure.

    attempts: [{"id", "outcome" ('completed' | 'failed' | 'cancelled' | 'open'),
                "hours" (float or None), "usage" (None when no usage was recorded, else
                [{"spool_id", "grams", "price", "net_weight_g", "provenance", "reason"}])}]
    where price / net_weight_g are the spool's purchase price and net filament weight as frozen
    when the attempt started, and "reason" says why they are missing.

    Returns state 'none' (no attempts: nothing was recorded), 'complete' or 'partial'. For a
    partial result `total` is the sum of what is known, to be shown as "at least", and `unknown`
    lists each missing fact as {"part", "reason", "attempt_id", "spool_id"}.
    """
    if not attempts:
        return {"state": "none"}
    from modules.models_library.services import price_one_item
    unknown = []
    try:
        hour = price_one_item(config, material_cost=0.0, print_hours=1.0)
        rate = hour["electricity"] + hour["depreciation"]
        settings = {key: hour[key] for key in ("labor", "packaging", "other", "overhead")}
        if not _amount(rate) or not all(_amount(value) for value in settings.values()):
            raise ValueError("pricing settings out of range")
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        rate, settings = None, None
        unknown.append({"part": "settings", "reason": "settings_incomplete", "attempt_id": None, "spool_id": None})

    spools, rows = {}, []
    material_total = machine_total = hours_total = failed_total = 0.0
    material_known = failed_known = True
    machine_known = rate is not None
    for attempt in attempts:
        missing = []
        material = 0.0
        material_ok = True
        if attempt.get("outcome") not in ATTEMPT_OUTCOMES:
            missing.append({"part": "attempt", "reason": "attempt_not_finished"})
            material_ok = False
        elif attempt.get("usage") is None:
            missing.append({"part": "material", "reason": "usage_not_recorded"})
            material_ok = False
        else:
            for use in attempt["usage"]:
                grams, price, net = use.get("grams"), use.get("price"), use.get("net_weight_g")
                line = spools.setdefault(use.get("spool_id"), {"spool_id": use.get("spool_id"), "grams": 0.0, "amount": 0.0,
                                                                "known": True, "provenance": use.get("provenance")})
                if line["provenance"] != use.get("provenance"):
                    line["provenance"] = "mixed"
                if not _amount(grams):
                    missing.append({"part": "material", "reason": "usage_not_recorded", "spool_id": use.get("spool_id")})
                    material_ok = line["known"] = False
                    continue
                line["grams"] += grams
                if not _amount(price) or not _amount(net) or net <= 0:
                    missing.append({"part": "material", "reason": use.get("reason") or "price_not_recorded",
                                    "spool_id": use.get("spool_id")})
                    material_ok = line["known"] = False
                    continue
                cost = grams * price / net
                line["amount"] += cost
                material += cost
        hours = attempt.get("hours")
        machine = None
        if attempt.get("outcome") in ATTEMPT_OUTCOMES:
            if not _amount(hours):
                missing.append({"part": "machine", "reason": "print_time_not_recorded"})
            elif rate is not None:
                machine = hours * rate
                hours_total += hours
        material_total += material
        material_known = material_known and material_ok
        machine_known = machine_known and machine is not None
        machine_total += machine or 0.0
        known = material + (machine or 0.0)
        whole = material_ok and machine is not None
        if attempt.get("outcome") != "completed":
            failed_total += known
            failed_known = failed_known and whole
        for item in missing:
            unknown.append({"part": item["part"], "reason": item["reason"], "attempt_id": attempt.get("id"),
                            "spool_id": item.get("spool_id")})
        rows.append({"id": attempt.get("id"), "outcome": attempt.get("outcome"),
                     "hours": round(hours, 4) if _amount(hours) else None,
                     "material": _money(material), "machine": _money(machine), "amount": _money(known),
                     "complete": whole})
    settings_total = sum(settings.values()) if settings else 0.0
    complete = not unknown
    total = material_total + machine_total + settings_total
    return {
        "state": "complete" if complete else "partial",
        "total": _money(total),
        "unknown": unknown,
        "material": {"amount": _money(material_total), "complete": material_known,
                     "spools": [{"spool_id": line["spool_id"], "grams": round(line["grams"], 2),
                                 "amount": _money(line["amount"]) if line["known"] else None,
                                 "provenance": line["provenance"]} for line in spools.values()]},
        "machine": {"amount": _money(machine_total), "complete": machine_known, "hours": round(hours_total, 4),
                    "rate_per_hour": None if rate is None else round(rate, 4), "basis": "current_rates"},
        "from_settings": {"amount": _money(settings_total) if settings else None, "complete": settings is not None,
                          "basis": "settings",
                          **{key: (_money(settings[key]) if settings else None)
                             for key in ("labor", "packaging", "other", "overhead")}},
        "failed_attempts": {"amount": _money(failed_total), "complete": failed_known,
                            "count": sum(1 for row in rows if row["outcome"] in ("failed", "cancelled"))},
        "attempts": rows,
        # Unrounded, for averages over many jobs (cost_per_print); not part of the API response.
        "_exact": {"total": total, "failed": failed_total},
    }


def _decoded(value):
    if isinstance(value, (str, bytes, bytearray)):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _number(value):
    """A frozen purchase figure (canonical decimal string) or a stored float, else None."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _frozen_purchase(snapshot):
    """{spool_id: (price, net_weight_g, reason)} written when the attempt started."""
    plan = snapshot.get("material_plan") if isinstance(snapshot, dict) else None
    allocations = plan.get("allocations") if isinstance(plan, dict) else None
    frozen = {}
    for item in allocations if isinstance(allocations, list) else []:
        if not isinstance(item, dict) or type(item.get("spool_id")) is not int or item["spool_id"] in frozen:
            continue
        inputs = item.get("purchase_inputs")
        if not isinstance(inputs, dict):
            continue
        price, net = _number(inputs.get("price")), _number(inputs.get("net_weight_g"))
        if inputs.get("state") != "recorded" or price is None or not net:
            reason = "spool_weight_missing" if "net_weight" in str(inputs.get("reason")) else "spool_price_missing"
            frozen[item["spool_id"]] = (None, None, reason)
        else:
            frozen[item["spool_id"]] = (price, net, None)
    return frozen


def _hours(duration_seconds, started_at, ended_at):
    """Printer-reported print time, else the recorded start-to-end span; None when neither exists."""
    if _amount(duration_seconds):
        return duration_seconds / 3600
    if started_at is None or ended_at is None:
        return None
    from core.db_compat import as_utc_datetime
    try:
        span = (as_utc_datetime(ended_at) - as_utc_datetime(started_at)).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return None
    return span / 3600 if span >= 0 else None


def _rows(db, statement, ids):
    found = []
    for start in range(0, len(ids), _CHUNK):
        found.extend(db.execute(text(statement).bindparams(bindparam("ids", expanding=True)),
                                {"ids": ids[start:start + _CHUNK]}).mappings().all())
    return found


def load_attempts(db, job_ids):
    """{job_id: [attempt, ...]} in cost_actual's input shape, for jobs the caller may already see.

    Three statements per 500 jobs whatever the number of jobs or attempts. Usage comes from the
    attempt's own accounting record (print_archives.consumption_json); an attempt accounted by
    its printer monitor has no such list and is read from the filament ledger rows that name the
    attempt. Purchase price and net weight come only from the snapshot taken at first start.
    """
    job_ids = sorted({int(identifier) for identifier in job_ids})
    result = {identifier: [] for identifier in job_ids}
    if not job_ids:
        return result
    attempts = _rows(db, "SELECT id,scheduled_job_id,status,started_at,ended_at,print_duration_seconds,spool_snapshot "
                         "FROM print_jobs WHERE scheduled_job_id IN :ids ORDER BY id", job_ids)
    attempt_ids = [row["id"] for row in attempts]
    archived, ledger = {}, {}
    if attempt_ids:
        for row in _rows(db, "SELECT print_job_id,accounting_state,consumption_json FROM print_archives "
                             "WHERE print_job_id IN :ids ORDER BY id", attempt_ids):
            archived.setdefault(row["print_job_id"], row)
        from core.filament_ledger import row_kind
        for row in _rows(db, "SELECT print_job_id,spool_id,weight_used_g,notes FROM spool_usage "
                             "WHERE print_job_id IN :ids ORDER BY id", attempt_ids):
            if row_kind(row["notes"]) in ("ledger", "auto"):
                spools = ledger.setdefault(row["print_job_id"], {})
                spools[row["spool_id"]] = spools.get(row["spool_id"], 0.0) + (row["weight_used_g"] or 0.0)
    for row in attempts:
        frozen = _frozen_purchase(_decoded(row["spool_snapshot"]))
        archive = archived.get(row["id"])
        entries = _decoded(archive["consumption_json"]) if archive and archive["accounting_state"] in _ACCOUNTED else None
        usage = None
        if isinstance(entries, list) and entries and all(
                isinstance(item, dict) and type(item.get("spool_id")) is int for item in entries):
            usage = [{"spool_id": item["spool_id"], "grams": item.get("grams_used"),
                      "provenance": item.get("quantity_provenance")} for item in entries]
        elif row["id"] in ledger:
            usage = [{"spool_id": spool_id, "grams": grams, "provenance": None}
                     for spool_id, grams in sorted(ledger[row["id"]].items())]
        for use in usage or []:
            price, net, reason = frozen.get(use["spool_id"], (None, None, "price_not_recorded"))
            use.update(price=price, net_weight_g=net, reason=reason)
        result[row["scheduled_job_id"]].append({
            "id": row["id"], "outcome": row["status"] if row["status"] in ATTEMPT_OUTCOMES else "open",
            "hours": _hours(row["print_duration_seconds"], row["started_at"], row["ended_at"]),
            "usage": usage})
    return result


def public(result):
    """The API shape of a cost_actual result (internal exact sums removed)."""
    return {key: value for key, value in result.items() if not key.startswith("_")}


def job_cost(db, job, user=None):
    """Response body of GET /jobs/{id}/cost for a job the caller has already been allowed to see."""
    from modules.models_library.services import saved_pricing_config
    config, configured = saved_pricing_config(db)
    status = getattr(job.status, "value", job.status)
    body = {"job_id": job.id, "configured": configured, "finished": status in FINISHED_JOB_STATUSES}
    if not configured or not body["finished"]:
        return body
    result = public(cost_actual(load_attempts(db, [job.id])[job.id], config))
    estimate = job.estimated_cost if _amount(job.estimated_cost) else None
    body.update(result, estimate=_money(estimate), pieces=job.quantity_on_bed if _pieces(job.quantity_on_bed) else None)
    body["difference"] = (round(result["total"] - round(estimate, 2), 2)
                          if result["state"] == "complete" and estimate is not None else None)
    if result["state"] != "none":
        names = _spool_names(db, [line["spool_id"] for line in result["material"]["spools"]], user)
        for line in result["material"]["spools"]:
            line["name"] = names.get(line["spool_id"])
    return body


def _pieces(value):
    return type(value) is int and value >= 1


def _spool_names(db, spool_ids, user):
    from core.rbac import check_org_access
    ids = sorted({identifier for identifier in spool_ids if type(identifier) is int})
    if not ids:
        return {}
    rows = _rows(db, "SELECT s.id,s.org_id,f.brand,f.name,f.material FROM spools s "
                     "LEFT JOIN filament_library f ON f.id=s.filament_id WHERE s.id IN :ids", ids)
    return {row["id"]: " ".join(part for part in (row["brand"], row["name"]) if part) or row["material"]
            for row in rows if check_org_access(user, row["org_id"])}
