"""Capacity planning and guarded assignment for the scheduler."""
from datetime import datetime, timezone
from typing import Optional, Dict, Tuple
from sqlalchemy import text
from sqlalchemy.orm import Session
from core.base import JobStatus
from core.registry import registry
from core.order_production import prepare_job_write, reconcile_job_write
from modules.printers.models import Printer
from modules.jobs.models import Job


class SchedulerPlanning:
    def run(self, db: Session, start_date: Optional[datetime] = None) -> "SchedulerResult":
        """
        Run the scheduler on all pending jobs.

        Args:
            db: Database session
            start_date: Scheduling horizon start (defaults to now)

        Returns:
            SchedulerResult with assignments and metrics
        """
        from modules.jobs.scheduler import (SchedulerResult, PrinterState, SlotAssignment,
            _printer_authority, evaluate_submission_compatibility)
        result = SchedulerResult(success=True)

        # Default start to now, rounded up to next slot
        if start_date is None:
            start_date = self._round_up_to_next_slot(datetime.now(timezone.utc))

        # All generic production parents are locked in one sorted batch before
        # cleanup or assignment can dirty a child. Education keeps its own CAS.
        candidates = list(db.execute(text("""
            SELECT id FROM jobs WHERE status IN ('pending','scheduled')
        """)).scalars())
        generic_ids = [identifier for identifier in candidates if not self._is_education_job(db, identifier)]
        production_orders = prepare_job_write(db, generic_ids) if generic_ids else []

        # Proactive stale schedule cleanup — reset SCHEDULED jobs past their window
        self._cleanup_stale_schedules(db, candidate_ids=candidates)

        # Calculate total slots in horizon
        total_slots = self.config.horizon_days * 24 * (60 // self.slot_minutes)

        # Load printers
        raw_authorities = {
            row.id: _printer_authority(row.org_id, row.shared, row.is_active)
            for row in db.execute(text('SELECT id,org_id,shared,is_active FROM printers'))
        }
        claimed_printers = set(db.execute(text('SELECT printer_id FROM printer_dispatch_claims')).scalars())
        from core.printer_maintenance import hold_reason
        from modules.printers.services import dispatch_unsupported_reason
        printers = [p for p in db.query(Printer).filter(Printer.is_active.is_(True)).all()
                    if raw_authorities.get(p.id) is not None and p.id not in claimed_printers
                    and not hold_reason(db, p.id) and not dispatch_unsupported_reason(p)]
        if not printers:
            # Stale cleanup may have made an atomic Education lifecycle
            # correction even when there is no printer available for new work.
            result.success = False
            result.errors.append("No active printers found")
            eligible = list(db.execute(text("SELECT id FROM jobs WHERE status='pending' AND hold IS FALSE "
                "AND (charged_to_user_id IS NOT NULL OR submitted_by IS NOT NULL) "
                "AND NOT EXISTS (SELECT 1 FROM education_submissions s WHERE s.job_id=jobs.id)")).scalars())
            result.skipped_count = len(eligible)
            return self._finish_run(db, result, len(eligible), [(jid, "no_active_printers") for jid in eligible])
        
        # Initialize printer states
        printer_states: Dict[int, PrinterState] = {
            p.id: PrinterState(p, raw_authorities[p.id]) for p in printers
        }
        
        # Track slot usage: {(printer_id, slot_index): job_id or "SETUP"}
        usage_map: Dict[Tuple[int, int], str] = {}
        
        # Load locked jobs first (Completed, Printing, AND Scheduled)
        # This prevents double-booking!
        locked_jobs = db.query(Job).filter(
            Job.status.in_([JobStatus.COMPLETED, JobStatus.PRINTING, JobStatus.SCHEDULED]),
            Job.printer_id.isnot(None),
            Job.scheduled_start.isnot(None)
        ).all()
        
        for job in locked_jobs:
            if job.printer_id not in printer_states:
                continue
                
            state = printer_states[job.printer_id]
            start_slot = self._time_to_slot(job.scheduled_start, start_date)
            if job.effective_duration is None:
                if job.status == JobStatus.COMPLETED:
                    # Completed history cannot occupy future capacity solely
                    # because its original production count is unknown.
                    continue
                # A scheduled/printing job of unknown duration cannot free any
                # projected capacity safely. Keep this printer unavailable.
                for slot in range(total_slots):
                    usage_map[(job.printer_id, slot)] = str(job.id)
                result.errors.append(f'Printer {job.printer_id} has job {job.id} with unknown quantity; capacity cannot be established')
                continue
            duration_slots = max(1, int(job.effective_duration * (60 / self.slot_minutes)))
            end_slot = start_slot + duration_slots
            
            # Mark slots as used
            for s in range(max(0, start_slot), min(total_slots, end_slot)):
                usage_map[(job.printer_id, s)] = str(job.id)
            
            # Update printer state
            if job.colors_list:
                state.colors = [c.lower() for c in job.colors_list]
            state.job_count += 1
            state.last_item = job.item_name
            state.last_end_slot = max(state.last_end_slot, end_slot)
        
        # Get pending jobs to schedule
        pending_jobs = db.query(Job).filter(
            Job.status == JobStatus.PENDING,
            Job.hold.is_(False),
            Job.id.in_(candidates)
        ).order_by(
            # Same queue order as GET /jobs: priority, then drag position within
            # the priority (unplaced last), then age.
            Job.priority, Job.queue_position.is_(None), Job.queue_position, Job.created_at, Job.id
        ).populate_existing().all()
        
        from modules.jobs.education_order import education_order
        education_ids = set(db.execute(text("SELECT job_id FROM education_submissions")).scalars())
        pending_jobs = education_order(pending_jobs, education_ids)

        # Pre-fetch printer model requirements from uploaded print files.
        # This is the safety gate data: a job that uses an H2D file must not
        # be assigned to an X1C printer. We look up print_files.printer_model
        # via the job's model_id (print_files.model_id FK). We only block on
        # known mismatches — if a printer's model is unset we let it through.
        job_model_requirements: Dict[int, str] = {}
        model_ids = [j.model_id for j in pending_jobs if j.model_id]
        if model_ids:
            placeholders = ",".join(str(m) for m in set(model_ids))
            rows = db.execute(text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
                f"SELECT model_id, printer_model FROM print_files "
                f"WHERE model_id IN ({placeholders}) "
                f"AND printer_model IS NOT NULL AND printer_model != '' "
                f"ORDER BY id DESC"
            )).fetchall()
            # Keep first (most recent) result per model_id
            seen: set = set()
            for row in rows:
                if row[0] not in seen:
                    job_model_requirements[row[0]] = row[1]
                    seen.add(row[0])

        # Record outcomes only for jobs ultimately left pending and not held.
        skipped = []
        # Schedule each job
        education_policy = registry.get_provider("EducationPolicyProvider")
        for job in pending_jobs:
            assigned = db.get(Printer, job.printer_id) if job.printer_id is not None else None
            unsupported = dispatch_unsupported_reason(assigned)
            if unsupported:
                result.skipped_count += 1
                skipped.append((job.id, 'printer_dispatch_unsupported'))
                result.errors.append(unsupported)
                continue
            if job.effective_duration is None:
                result.skipped_count += 1
                skipped.append((job.id, "unknown_quantity"))
                result.errors.append(f'Could not schedule job {job.id}: quantity is unknown')
                continue
            education_context = None
            approved_printer_id = None
            if self._is_education_job(db, job.id):
                if education_policy is None:
                    result.skipped_count += 1
                    skipped.append((job.id, "policy_unavailable"))
                    result.errors.append(
                        f"Could not schedule Education job {job.id}: policy unavailable"
                    )
                    continue
                education_context = education_policy.scheduler_context(db, job_id=job.id)
                if not education_context:
                    result.skipped_count += 1
                    skipped.append((job.id, "policy_context_unavailable"))
                    result.errors.append(
                        f"Could not schedule Education job {job.id}: policy context unavailable"
                    )
                    continue
                if not education_context.get("authorized"):
                    changed = education_policy.reconcile_schedule_denial(
                        db,
                        submission_id=int(education_context["id"]),
                        job_id=job.id,
                        expected_revision=int(education_context["lifecycle_revision"]),
                        reason="scheduler_policy_denied",
                    )
                    if changed:
                        self._mark_reconciled_job(job)
                    result.skipped_count += 1
                    skipped.append((job.id, "policy_denied"))
                    result.errors.append(
                        f"Could not schedule Education job {job.id}: policy denied"
                    )
                    continue
                approved_printer_id = int(education_context["approved_printer_id"])
                compatibility = evaluate_submission_compatibility(
                    db,
                    org_id=int(education_context["org_id"]),
                    print_file_id=int(education_context["print_file_id"]),
                    printer_id=approved_printer_id,
                )
                if not compatibility or not compatibility.get("compatible"):
                    reason = self._compatibility_denial_reason(compatibility)
                    changed = education_policy.reconcile_schedule_denial(
                        db,
                        submission_id=int(education_context["id"]),
                        job_id=job.id,
                        expected_revision=int(education_context["lifecycle_revision"]),
                        reason=reason,
                    )
                    if changed:
                        self._mark_reconciled_job(job)
                    result.skipped_count += 1
                    skipped.append((job.id, "compatibility_denied"))
                    result.errors.append(
                        f"Could not schedule Education job {job.id}: {reason}"
                    )
                    continue

            # Generic specific-printer pin, chosen explicitly by a user and kept
            # across resets. A missing/inactive pinned printer leaves the job
            # pending with a visible reason (never placed elsewhere).
            # A deleted pin waits for a new choice (never retargeted onto a reused id);
            # a legacy unparseable target is no pin. The reason is the alert reason.
            from modules.jobs.pinning import job_pin, job_issue, BLOCKING
            if education_context is None:
                pin, issue = job_pin(job), job_issue(db, job)
                from core.print_file_binding import job_file_id, target_printer
                bound = target_printer(db, job_file_id(db, job.model_id, job.print_file_id))
                if bound is not None:
                    if pin is not None and pin != bound:
                        result.skipped_count += 1
                        skipped.append((job.id, "upload_binding_mismatch"))
                        result.errors.append(f"Could not schedule job {job.id}: print file is bound to printer {bound}")
                        continue
                    approved_printer_id = bound
                if pin is not None:
                    approved_printer_id = pin
                if issue in BLOCKING:
                    result.skipped_count += 1
                    skipped.append((job.id, issue))
                    result.errors.append(
                        f"Could not schedule job {job.id}: pinned printer {pin if pin is not None else ''} is unavailable ({issue})")
                    continue

            payload_snapshot = None
            if education_context is None:
                from core.scheduler_payload_authority import capture, assignment_guard
                payload_snapshot = capture(db, job.id)
                if payload_snapshot is None:
                    result.skipped_count += 1
                    skipped.append((job.id, "current_state_changed"))
                    result.errors.append(f'Could not schedule job {job.id}: payload authority could not be verified')
                    continue

            best_fit = self._find_best_fit(
                job=job,
                printer_states=printer_states,
                usage_map=usage_map,
                start_date=start_date,
                total_slots=total_slots,
                # Education already passed the provenance-aware compatibility
                # evaluator above. Its canonical aliases must not be rejected
                # by the legacy display-model string comparison; keep the pin.
                required_printer_model=(
                    None if education_context is not None
                    else job_model_requirements.get(job.model_id)
                ),
                required_printer_id=approved_printer_id,
            )
            
            if best_fit:
                if education_context is not None:
                    advanced = education_policy.advance_schedule(
                        db,
                        submission_id=int(education_context["id"]),
                        job_id=job.id,
                        printer_id=int(best_fit["printer_id"]),
                        expected_revision=int(education_context["lifecycle_revision"]),
                    )
                    if not advanced:
                        result.skipped_count += 1
                        skipped.append((job.id, "revision_changed"))
                        result.errors.append(
                            f"Could not schedule Education job {job.id}: revision changed"
                        )
                        continue
                else:
                    payload_guard, payload_parameters = assignment_guard(payload_snapshot)
                    from modules.jobs.state_machine import transition_job
                    changed = transition_job(db, job.id, JobStatus.SCHEDULED, "scheduler",
                        extra_fields={"printer_id": best_fit['printer_id'], "scheduled_start": best_fit['start_time'],
                                      "scheduled_end": best_fit['end_time'], "match_score": best_fit['match_score']},
                        where=f"""hold IS FALSE
                          AND quantity=:quantity
                          AND {payload_guard}
                          AND NOT EXISTS (SELECT 1 FROM printer_dispatch_claims dc WHERE dc.printer_id=:printer)
                          AND (charged_to_org_id=:tenant OR (charged_to_org_id IS NULL AND CAST(:tenant AS INTEGER) IS NULL))
                          AND EXISTS (SELECT 1 FROM printers p WHERE p.id=:printer
                            AND p.is_active=:enabled
                            AND (p.org_id=:owner OR (p.org_id IS NULL AND CAST(:owner AS INTEGER) IS NULL))
                            AND (p.shared=:sharing OR (p.shared IS NULL AND CAST(:sharing AS BOOLEAN) IS NULL))
                            AND (p.org_id IS NULL OR p.org_id=jobs.charged_to_org_id OR p.shared=:enabled))
                          AND NOT EXISTS (SELECT 1 FROM education_submissions s WHERE s.job_id=jobs.id)""",
                        params={'printer':best_fit['printer_id'], **payload_parameters, 'id': job.id,
                                'quantity':job.quantity, 'tenant':job.charged_to_org_id, 'enabled':True,
                                'owner':printer_states[best_fit['printer_id']].authority['org_id'],
                                'sharing':printer_states[best_fit['printer_id']].authority['stored_shared']}) or None
                    if changed is None:
                        db.expire(job)
                        result.skipped_count += 1
                        skipped.append((job.id, "current_state_changed"))
                        result.errors.append(f'Could not schedule job {job.id}: current state changed')
                        continue
                    db.expire(job)
                # Reserve projected capacity only after the database accepted it.
                self._apply_assignment(job=job, fit=best_fit,
                                       printer_states=printer_states, usage_map=usage_map)
                if education_context is not None:
                    # The Education policy already wrote this transition in SQL.
                    from modules.jobs.state_machine import align_identity
                    align_identity(job, JobStatus.SCHEDULED, printer_id=best_fit['printer_id'],
                                   scheduled_start=best_fit['start_time'], scheduled_end=best_fit['end_time'],
                                   match_score=best_fit['match_score'])

                # Track metrics
                result.scheduled_count += 1
                result.total_match_score += best_fit["match_score"]
                result.total_duration += job.effective_duration
                if best_fit["requires_setup"]:
                    result.setup_blocks += 1
                
                # Record assignment
                result.assignments.append(SlotAssignment(
                    printer_id=best_fit["printer_id"],
                    printer_name=best_fit["printer_name"],
                    job_id=job.id,
                    start_slot=best_fit["start_slot"],
                    end_slot=best_fit["end_slot"],
                    start_time=best_fit["start_time"],
                    end_time=best_fit["end_time"],
                    match_score=best_fit["match_score"]
                ))
            else:
                result.skipped_count += 1
                skipped.append((job.id, "no_fit"))
                req = job_model_requirements.get(job.model_id)
                if req:
                    result.errors.append(f"Could not schedule job {job.id} ({job.item_name}): requires {req} printer, none available")
                else:
                    result.errors.append(f"Could not schedule job {job.id}: {job.item_name}")
        
        # Reconcile in the same transaction as all generic state writes.
        reconcile_job_write(db, production_orders)
        return self._finish_run(db, result, len(pending_jobs), skipped)

