"""
O.D.I.N. — Scheduler Engine

This is the brain of the operation - it assigns pending jobs to printers
while optimizing for minimal filament changes.

Ported from the Google Apps Script logic with improvements.
"""

from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field
from sqlalchemy.orm import Session

from models import Printer, Job, JobStatus, SchedulerRun


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


class PrinterState:
    """Tracks the current state of a printer during scheduling."""
    
    def __init__(self, printer: Printer):
        self.printer = printer
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
        """Convert a datetime to a slot index."""
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
    
    def _cleanup_stale_schedules(self, db: Session) -> int:
        """Reset SCHEDULED jobs whose time window has passed (>2hrs past scheduled_start)."""
        cutoff = datetime.now() - timedelta(hours=2)
        stale = db.query(Job).filter(
            Job.status == JobStatus.SCHEDULED,
            Job.scheduled_start < cutoff
        ).all()
        for job in stale:
            job.status = JobStatus.PENDING
            job.printer_id = None
            job.scheduled_start = None
            job.scheduled_end = None
            job.match_score = None
        if stale:
            db.flush()
        return len(stale)

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
            start_date = self._round_up_to_next_slot(datetime.now())

        # Proactive stale schedule cleanup — reset SCHEDULED jobs past their window
        self._cleanup_stale_schedules(db)

        # Calculate total slots in horizon
        total_slots = self.config.horizon_days * 24 * (60 // self.slot_minutes)

        # Load printers
        printers = db.query(Printer).filter(Printer.is_active.is_(True)).all()
        if not printers:
            result.success = False
            result.errors.append("No active printers found")
            return result
        
        # Initialize printer states
        printer_states: Dict[int, PrinterState] = {
            p.id: PrinterState(p) for p in printers
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
            Job.hold == False
        ).order_by(Job.priority, Job.created_at).all()
        
        # Schedule each job
        for job in pending_jobs:
            best_fit = self._find_best_fit(
                job=job,
                printer_states=printer_states,
                usage_map=usage_map,
                start_date=start_date,
                total_slots=total_slots
            )
            
            if best_fit:
                # Apply the assignment
                self._apply_assignment(
                    job=job,
                    fit=best_fit,
                    printer_states=printer_states,
                    usage_map=usage_map
                )
                
                # Update the job in database
                job.status = JobStatus.SCHEDULED
                job.printer_id = best_fit["printer_id"]
                job.scheduled_start = best_fit["start_time"]
                job.scheduled_end = best_fit["end_time"]
                job.match_score = best_fit["match_score"]
                
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
                result.errors.append(f"Could not schedule job {job.id}: {job.item_name}")
        
        # Commit changes
        db.commit()
        
        # Log the run
        run_log = SchedulerRun(
            total_jobs=len(pending_jobs),
            scheduled_count=result.scheduled_count,
            skipped_count=result.skipped_count,
            setup_blocks=result.setup_blocks,
            avg_match_score=result.avg_match_score,
            avg_job_duration=result.avg_duration,
            notes="; ".join(result.errors[:5]) if result.errors else None
        )
        db.add(run_log)
        db.commit()
        
        return result
    
    def _find_best_fit(
        self,
        job: Job,
        printer_states: Dict[int, PrinterState],
        usage_map: Dict[Tuple[int, int], str],
        start_date: datetime,
        total_slots: int
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

        required_colors = job.colors_list or []
        required_tags = job.required_tags or []
        duration_slots = max(1, int(job.effective_duration * (60 / self.slot_minutes)))

        for printer_id, state in printer_states.items():
            # Tag constraint: skip printers missing required tags
            if required_tags:
                printer_tags = state.printer.tags or []
                if not all(t in printer_tags for t in required_tags):
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
    """Convenience function to run the scheduler."""
    scheduler = Scheduler(config)
    return scheduler.run(db)
