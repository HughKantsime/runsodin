"""
O.D.I.N. — Scheduler Engine

This is the brain of the operation - it assigns pending jobs to printers
while optimizing for minimal filament changes.

Ported from the Google Apps Script logic with improvements.
"""

from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field
from sqlalchemy.orm import Session
from sqlalchemy import text

from core.base import JobStatus, AlertType, AlertSeverity
from core.registry import registry
from core.order_production import prepare_job_write, reconcile_job_write
from modules.printers.models import Printer
from modules.printers.services import evaluate_submission_compatibility
from modules.jobs.models import Job, SchedulerRun


def _printer_identity(printer) -> str:
    """The printer's model identity: auto-detected machine_type wins over the
    (possibly stale, user-entered) model; model only when machine_type is empty."""
    machine_type = (getattr(printer, 'machine_type', None) or '').strip()
    return machine_type or (getattr(printer, 'model', None) or '').strip()


def _same_printer_model(printer_identity: str, required_model: str) -> bool:
    """True only when the file's sliced-for model and the printer are the same machine.

    Both sides resolve through the single Bambu model table
    (core.printer_identity.canonical_machine over printer_models.BAMBU_MODEL_IDS).
    Fails closed: when either side is unknown to the table it never matches a
    different model; only a byte-identical identifier (case-insensitive) counts.
    """
    from core.printer_identity import canonical_machine
    installed_raw = str(printer_identity or '').strip()
    required_raw = str(required_model or '').strip()
    installed, required = canonical_machine(installed_raw), canonical_machine(required_raw)
    if installed is not None and required is not None:
        return installed == required
    if installed is not None or required is not None:
        return False
    return bool(installed_raw) and installed_raw.casefold() == required_raw.casefold()


@dataclass
class SchedulerConfig:
    """Configuration for the scheduler."""
    blackout_start_hour: int = 22
    blackout_start_minute: int = 30
    blackout_end_hour: int = 5
    blackout_end_minute: int = 30
    setup_duration_slots: int = 1  # 15-min slots for color change
    slot_duration_minutes: int = 15
    horizon_days: int = 7
    
    @classmethod
    def from_time_strings(cls, blackout_start: str = "22:30", blackout_end: str = "05:30", **kwargs):
        """Create config from HH:MM time strings."""
        start_h, start_m = map(int, blackout_start.split(":"))
        end_h, end_m = map(int, blackout_end.split(":"))
        return cls(
            blackout_start_hour=start_h,
            blackout_start_minute=start_m,
            blackout_end_hour=end_h,
            blackout_end_minute=end_m,
            **kwargs
        )


@dataclass
class SlotAssignment:
    """Represents a job assigned to a time slot."""
    printer_id: int
    printer_name: str
    job_id: int
    start_slot: int
    end_slot: int
    start_time: datetime
    end_time: datetime
    is_setup: bool = False
    match_score: int = 0


@dataclass 
class SchedulerResult:
    """Result of a scheduler run."""
    success: bool
    scheduled_count: int = 0
    skipped_count: int = 0
    setup_blocks: int = 0
    total_match_score: int = 0
    total_duration: float = 0
    assignments: List[SlotAssignment] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    
    @property
    def avg_match_score(self) -> float:
        if self.scheduled_count == 0:
            return 0
        return self.total_match_score / self.scheduled_count
    
    @property
    def avg_duration(self) -> float:
        if self.scheduled_count == 0:
            return 0
        return self.total_duration / self.scheduled_count


def _printer_authority(org_id, shared, active):
    """Decode database permission flags without permissive ORM coercion."""
    def flag(value, nullable=False):
        if value is None and nullable: return False
        if type(value) is bool: return value
        if type(value) is int and value in (0, 1): return bool(value)
        return None
    enabled = flag(active)
    sharing = flag(shared, True)
    if enabled is not True or sharing is None:
        return None
    if org_id is not None and (type(org_id) is not int or org_id <= 0):
        return None
    return {'org_id': org_id, 'shared': sharing, 'stored_shared': shared}


class PrinterState:
    """Tracks the current state of a printer during scheduling."""
    
    def __init__(self, printer: Printer, authority=None):
        self.printer = printer
        self.authority = authority or _printer_authority(printer.org_id, printer.shared, printer.is_active)
        self.id = printer.id
        self.name = printer.name
        self.colors = [c.lower() for c in printer.loaded_colors] if printer.loaded_colors else []
        self.job_count = 0
        self.last_item: Optional[str] = None
        self.last_end_slot: int = 0
    
    def update_colors(self, new_colors: List[str]):
        """Update loaded colors after a job."""
        self.colors = [c.lower() for c in new_colors] if new_colors else []


class Scheduler:
    """
    The main scheduler that assigns jobs to printers.
    
    Key optimization: minimize filament changes by batching similar-color jobs.
    Color matching is a PREFERENCE, not a requirement - jobs always get scheduled.
    """
    
    def __init__(self, config: Optional[SchedulerConfig] = None):
        self.config = config or SchedulerConfig()
        self.slot_minutes = self.config.slot_duration_minutes
        
    def _time_to_slot(self, dt: datetime, start_date: datetime) -> int:
        """Convert a datetime to a slot index; persisted naive times are UTC."""
        # SQLite reloads DateTime columns without tzinfo. Scheduling writes UTC,
        # so restore that interpretation without changing aware timestamp offsets.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if start_date.tzinfo is None:
            start_date = start_date.replace(tzinfo=timezone.utc)
        delta = dt - start_date
        return int(delta.total_seconds() / (self.slot_minutes * 60))
    
    def _slot_to_time(self, slot: int, start_date: datetime) -> datetime:
        """Convert a slot index to a datetime."""
        return start_date + timedelta(minutes=slot * self.slot_minutes)
    
    def _round_up_to_next_slot(self, dt: datetime) -> datetime:
        """Round up to the next slot boundary."""
        minutes = dt.minute
        remainder = minutes % self.slot_minutes
        if remainder != 0:
            dt = dt + timedelta(minutes=self.slot_minutes - remainder)
        return dt.replace(second=0, microsecond=0)
    
    def _is_blackout_time(self, dt: datetime) -> bool:
        """Check if a time falls within the blackout window."""
        minutes = dt.hour * 60 + dt.minute
        blackout_start = self.config.blackout_start_hour * 60 + self.config.blackout_start_minute
        blackout_end = self.config.blackout_end_hour * 60 + self.config.blackout_end_minute
        
        # Handle overnight blackout (e.g., 22:30 to 05:30)
        if blackout_start > blackout_end:
            return minutes >= blackout_start or minutes < blackout_end
        else:
            return blackout_start <= minutes < blackout_end
    
    def _calculate_color_score(self, loaded_colors: List[str], required_colors: List[str]) -> int:
        """
        Calculate how well a printer's loaded colors match a job's requirements.
        Returns 0-100 score (100 = perfect match).
        """
        if not required_colors:
            return 50  # Neutral score for jobs with no color requirements
            
        loaded_set = set(c.lower() for c in loaded_colors) if loaded_colors else set()
        required_set = set(c.lower() for c in required_colors)
        
        if not required_set:
            return 50
            
        matched = len(required_set & loaded_set)
        total_required = len(required_set)
        
        # Score is percentage of required colors that are loaded
        return int((matched / total_required) * 100)
    
    def _requires_setup(self, loaded_colors: List[str], required_colors: List[str]) -> bool:
        """Check if a color change is needed."""
        if not required_colors:
            return False  # No colors required = no setup needed
            
        loaded_set = set(c.lower() for c in loaded_colors) if loaded_colors else set()
        required_set = set(c.lower() for c in required_colors)
        
        return not required_set.issubset(loaded_set)
    
    def _find_first_available_slot(
        self,
        printer_id: int,
        duration_slots: int,
        usage_map: Dict[Tuple[int, int], str],
        start_date: datetime,
        total_slots: int,
        search_start: int = 0
    ) -> Optional[int]:
        """Find the first available slot for a job on a specific printer."""
        for start_slot in range(search_start, total_slots - duration_slots + 1):
            start_time = self._slot_to_time(start_slot, start_date)
            
            # Skip blackout times for the START of the job
            if self._is_blackout_time(start_time):
                continue
            
            # Check for conflicts across all needed slots
            conflict = False
            for s in range(start_slot, start_slot + duration_slots):
                if (printer_id, s) in usage_map:
                    conflict = True
                    break
            
            if not conflict:
                return start_slot
        
        return None
    
    @staticmethod
    def _is_education_job(db: Session, job_id: int) -> bool:
        """Return whether a job is owned by the Education workflow."""
        return db.execute(
            text("SELECT 1 FROM education_submissions WHERE job_id=:job_id LIMIT 1"),
            {"job_id": job_id},
        ).fetchone() is not None

    @staticmethod
    def _compatibility_denial_reason(compatibility: dict | None) -> str:
        """Build a bounded, non-secret scheduler reason from compatibility codes."""
        if not compatibility:
            return "compatibility_unavailable"
        codes = sorted(
            {
                str(reason.get("code") or "compatibility_denied")[:64]
                for reason in compatibility.get("reasons", [])
            }
        )
        return ",".join(codes)[:160] or "compatibility_denied"

    @staticmethod
    def _mark_reconciled_job(job: Job) -> None:
        """Keep the ORM identity map aligned with a central-policy reconciliation (no write)."""
        from modules.jobs.state_machine import align_identity
        align_identity(job, JobStatus.SUBMITTED, printer_id=None, scheduled_start=None,
                       scheduled_end=None, match_score=None)

    def _cleanup_stale_schedules(self, db: Session, candidate_ids=None) -> int:
        """Reset stale schedules while preserving Education lifecycle policy."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
        query = db.query(Job).filter(Job.status == JobStatus.SCHEDULED,
                                     Job.scheduled_start < cutoff)
        if candidate_ids is not None:
            query = query.filter(Job.id.in_(candidate_ids))
        stale = query.populate_existing().all()
        generic_ids = [job.id for job in stale if not self._is_education_job(db, job.id)]
        parents = prepare_job_write(db, generic_ids) if generic_ids else []
        policy = registry.get_provider("EducationPolicyProvider")
        reset_count = 0
        for job in stale:
            if self._is_education_job(db, job.id):
                # An Education job must never fall through to generic cleanup. If
                # its provider is unavailable, leave it scheduled and fail closed.
                if policy is None:
                    continue
                context = policy.scheduler_context(db, job_id=job.id)
                if not context:
                    continue
                printer_id = context.get("approved_printer_id")
                compatibility = None
                if context.get("authorized") and printer_id is not None:
                    compatibility = evaluate_submission_compatibility(
                        db,
                        org_id=int(context["org_id"]),
                        print_file_id=int(context["print_file_id"]),
                        printer_id=int(printer_id),
                    )
                if compatibility and compatibility.get("compatible"):
                    changed = policy.reset_stale_schedule(
                        db,
                        submission_id=int(context["id"]),
                        job_id=job.id,
                        printer_id=int(printer_id),
                        expected_revision=int(context["lifecycle_revision"]),
                    )
                    if changed:
                        from modules.jobs.state_machine import align_identity
                        align_identity(job, JobStatus.PENDING, printer_id=int(printer_id),
                                       scheduled_start=None, scheduled_end=None, match_score=None)
                        reset_count += 1
                    continue

                reason = (
                    "scheduler_policy_denied"
                    if not context.get("authorized")
                    else self._compatibility_denial_reason(compatibility)
                )
                changed = policy.reconcile_schedule_denial(
                    db,
                    submission_id=int(context["id"]),
                    job_id=job.id,
                    expected_revision=int(context["lifecycle_revision"]),
                    reason=reason,
                )
                if changed:
                    self._mark_reconciled_job(job)
                    reset_count += 1
                continue

            from modules.jobs.state_machine import transition_job
            changed = transition_job(db, job.id, JobStatus.PENDING, "scheduler",
                from_states=(JobStatus.SCHEDULED,),
                extra_fields={"printer_id": None, "scheduled_start": None,
                              "scheduled_end": None, "match_score": None},
                where="scheduled_start < :cutoff AND NOT EXISTS "
                      "(SELECT 1 FROM education_submissions s WHERE s.job_id=jobs.id)",
                params={'cutoff': cutoff})
            db.expire(job)
            if changed:
                reset_count += 1
        if reset_count:
            db.flush()
        reconcile_job_write(db, parents)
        return reset_count

    @staticmethod
    def _finish_run(db: Session, result: SchedulerResult, total_jobs: int, skipped):
        from modules.notifications.alert_dispatcher import lock_queue_alert, stage_alert, deliver_staged_alert
        plans = []
        messages = {
            "unknown_quantity": "Set the job quantity before scheduling. The job remains queued.",
            "no_fit": "No compatible printer has capacity within the scheduling horizon. Check job requirements and printer availability.",
            "no_active_printers": "No active printers are available. Check which printers are enabled; the job remains queued.",
            "policy_unavailable": "The Education scheduling policy is unavailable. Check server diagnostics before scheduling.",
            "policy_context_unavailable": "The Education approval context could not be validated. Review the submission before scheduling.",
            "policy_denied": "The current Education policy does not authorize scheduling. Review the submission approval.",
            "compatibility_denied": "The approved printer no longer meets the submission requirements. Review compatibility and approval.",
            "revision_changed": "The approval changed during scheduling. Review its current state before scheduling again.",
            "current_state_changed": "The job changed during scheduling. Review its current state before scheduling again.",
            "pinned_printer_inactive": "The printer this job is pinned to is inactive. Re-activate it or change the job's printer; the job remains queued.",
            "pinned_printer_missing": "The printer this job is pinned to no longer exists. Choose another printer; the job remains queued.",
            "pinned_printer_deleted": "The printer this job was pinned to was deleted. Choose another printer; the job remains queued.",
            "pinned_printer_inaccessible": "Your organization can no longer use the printer this job is pinned to. Choose another printer; the job remains queued.",
        }
        for job_id, reason in sorted(skipped, key=lambda item: item[0]):
            if not lock_queue_alert(db, job_id, AlertType.QUEUE_SKIPPED):
                continue
            row = db.execute(text("SELECT item_name,charged_to_user_id,submitted_by FROM jobs "
                "WHERE id=:id AND status='pending' AND hold IS FALSE"), {"id": job_id}).mappings().first()
            if not row:
                continue
            creator = row["charged_to_user_id"] or row["submitted_by"]
            if creator is None:
                continue
            plans.append(stage_alert(db, AlertType.QUEUE_SKIPPED, AlertSeverity.WARNING,
                f"Job could not be scheduled: {row['item_name'] or 'Untitled'}",
                messages[reason],
                job_id=job_id, metadata={"reason": reason}, target_user_ids=[creator]))
        db.add(SchedulerRun(total_jobs=total_jobs, scheduled_count=result.scheduled_count,
            skipped_count=result.skipped_count, setup_blocks=result.setup_blocks,
            avg_match_score=result.avg_match_score, avg_job_duration=result.avg_duration,
            notes="; ".join(result.errors[:5]) if result.errors else None))
        db.commit()
        for plan in plans:
            deliver_staged_alert(db, plan)
        return result

    def run(self, db: Session, start_date: Optional[datetime] = None) -> SchedulerResult:
        """
        Run the scheduler on all pending jobs.

        Args:
            db: Database session
            start_date: Scheduling horizon start (defaults to now)

        Returns:
            SchedulerResult with assignments and metrics
        """
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
        printers = [p for p in db.query(Printer).filter(Printer.is_active.is_(True)).all()
                    if raw_authorities.get(p.id) is not None and p.id not in claimed_printers]
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

    def _find_best_fit(
        self,
        job: Job,
        printer_states: Dict[int, PrinterState],
        usage_map: Dict[Tuple[int, int], str],
        start_date: datetime,
        total_slots: int,
        required_printer_model: Optional[str] = None,
        required_printer_id: Optional[int] = None,
    ) -> Optional[Dict]:
        """
        Find the best printer and time slot for a job.
        
        Strategy:
        1. Find first available slot on EACH printer
        2. Score each option (color match is a bonus, not requirement)
        3. Pick the best scored option
        
        Returns dict with assignment details or None if no fit found.
        """
        candidates = []
        if job.effective_duration is None:
            return None

        required_colors = job.colors_list or []
        required_tags = job.required_tags or []
        duration_slots = max(1, int(job.effective_duration * (60 / self.slot_minutes)))

        from core.dispatch_identity import ownership_refusal
        for printer_id, state in printer_states.items():
            if state.authority is None or ownership_refusal(
                    {'charged_to_org_id': job.charged_to_org_id}, state.authority):
                continue
            if required_printer_id is not None and printer_id != required_printer_id:
                continue

            # Model constraint for model-specific files: the printer's identity
            # (auto-detected machine_type, else model) must be the same machine
            # through the Bambu model table. Fail closed: a printer with no
            # identity at all never receives a model-specific file.
            if required_printer_model:
                identity = _printer_identity(state.printer)
                if not identity or not _same_printer_model(identity, required_printer_model):
                    continue

            # Tag constraint: skip printers missing required tags
            if required_tags:
                printer_tags = state.printer.tags or []
                if not all(t in printer_tags for t in required_tags):
                    continue

            # Target type constraint: filter by machine_type or protocol
            job_target_type = getattr(job, 'target_type', 'specific') or 'specific'
            job_target_filter = getattr(job, 'target_filter', None)
            if job_target_type == 'model' and job_target_filter:
                # Same single model table as file matching (X1C == "Bambu Lab X1 Carbon" == BL-P001).
                identity = _printer_identity(state.printer)
                if not identity or not _same_printer_model(identity, job_target_filter):
                    continue
            elif job_target_type == 'protocol' and job_target_filter:
                printer_protocol = getattr(state.printer, 'api_type', None) or ''
                if printer_protocol.lower() != job_target_filter.lower():
                    continue

            # Calculate color match score (0-100)
            color_score = self._calculate_color_score(state.colors, required_colors)
            requires_setup = self._requires_setup(state.colors, required_colors)
            
            # Add setup time if needed
            total_slots_needed = duration_slots
            if requires_setup:
                total_slots_needed += self.config.setup_duration_slots
            
            # Find first available slot on this printer
            start_slot = self._find_first_available_slot(
                printer_id=printer_id,
                duration_slots=total_slots_needed,
                usage_map=usage_map,
                start_date=start_date,
                total_slots=total_slots
            )
            
            if start_slot is None:
                continue  # No space on this printer
            
            # Calculate actual job start (after setup if needed)
            job_start_slot = start_slot + (self.config.setup_duration_slots if requires_setup else 0)
            job_start_time = self._slot_to_time(job_start_slot, start_date)
            end_slot = job_start_slot + duration_slots
            end_time = self._slot_to_time(end_slot, start_date)
            
            # Calculate priority score
            # Higher score = better fit
            score = 0
            
            # Color match bonus (0-100 points)
            score += color_score
            
            # No setup needed bonus
            if not requires_setup:
                score += 50
            
            # Earlier start time bonus (prefer filling gaps)
            score -= start_slot * 0.1
            
            # Spread load across printers (slight penalty for busy printers)
            score -= state.job_count * 5
            
            # Priority bonus (higher priority jobs = lower number = more important)
            score += (5 - job.priority) * 10
            
            candidates.append({
                "printer_id": printer_id,
                "printer_name": state.name,
                "start_slot": start_slot,
                "job_start_slot": job_start_slot,
                "end_slot": end_slot,
                "start_time": job_start_time,
                "end_time": end_time,
                "requires_setup": requires_setup,
                "match_score": color_score,
                "score": score
            })
        
        if not candidates:
            return None
        
        # Sort by score (highest first) and return best
        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[0]
    
    def _apply_assignment(
        self,
        job: Job,
        fit: Dict,
        printer_states: Dict[int, PrinterState],
        usage_map: Dict[Tuple[int, int], str]
    ):
        """Apply a job assignment to the state tracking structures."""
        printer_id = fit["printer_id"]
        state = printer_states[printer_id]
        
        # Mark setup slots if needed
        if fit["requires_setup"]:
            for s in range(fit["start_slot"], fit["job_start_slot"]):
                usage_map[(printer_id, s)] = "SETUP"
        
        # Mark job slots
        for s in range(fit["job_start_slot"], fit["end_slot"]):
            usage_map[(printer_id, s)] = str(job.id)
        
        # Update printer state
        if job.colors_list:
            state.colors = [c.lower() for c in job.colors_list]
        state.job_count += 1
        state.last_item = job.item_name
        state.last_end_slot = fit["end_slot"]


def run_scheduler(db: Session, config: Optional[SchedulerConfig] = None) -> SchedulerResult:
    """Run the scheduler; without an explicit config it uses the saved blackout hours."""
    if config is None:
        from core.scheduler_settings import get_blackout
        start, end = get_blackout(db)
        config = SchedulerConfig.from_time_strings(blackout_start=start, blackout_end=end)
    scheduler = Scheduler(config)
    return scheduler.run(db)
