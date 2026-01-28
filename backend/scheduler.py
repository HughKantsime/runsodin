"""
Scheduler Engine for PrintFarm

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
    setup_duration_slots: int = 1  # 30-min slots for color change
    slot_duration_minutes: int = 30
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
        self.colors = [c.lower() for c in printer.loaded_colors]
        self.job_count = 0
        self.last_item: Optional[str] = None
        self.last_end_slot: int = 0
    
    def update_colors(self, new_colors: List[str]):
        """Update loaded colors after a job."""
        self.colors = [c.lower() for c in new_colors]


class Scheduler:
    """
    The main scheduler that assigns jobs to printers.
    
    Key optimization: minimize filament changes by batching similar-color jobs.
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
        """Round up to the next slot boundary (e.g., :00 or :30)."""
        minutes = dt.minute
        if minutes % self.slot_minutes != 0:
            dt = dt + timedelta(minutes=self.slot_minutes - (minutes % self.slot_minutes))
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
        
        Scoring:
        - +25 for each color already loaded
        - -10 for each color that needs to be loaded
        - -5 for each extra color loaded that isn't needed
        """
        loaded_set = set(c.lower() for c in loaded_colors)
        required_set = set(c.lower() for c in required_colors)
        
        matched = len(required_set & loaded_set)
        missing = len(required_set - loaded_set)
        extra = len(loaded_set - required_set)
        
        return (matched * 25) - (missing * 10) - (extra * 5)
    
    def _requires_setup(self, loaded_colors: List[str], required_colors: List[str]) -> bool:
        """Check if a color change is needed."""
        loaded_set = set(c.lower() for c in loaded_colors)
        required_set = set(c.lower() for c in required_colors)
        return not required_set.issubset(loaded_set)
    
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
        
        # Calculate total slots in horizon
        total_slots = self.config.horizon_days * 24 * (60 // self.slot_minutes)
        
        # Load printers
        printers = db.query(Printer).filter(Printer.is_active == True).all()
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
        
        # Load and place locked jobs first (Completed, Printing)
        locked_jobs = db.query(Job).filter(
            Job.status.in_([JobStatus.COMPLETED, JobStatus.PRINTING]),
            Job.printer_id.isnot(None),
            Job.scheduled_start.isnot(None)
        ).all()
        
        for job in locked_jobs:
            if job.printer_id not in printer_states:
                continue
                
            state = printer_states[job.printer_id]
            start_slot = self._time_to_slot(job.scheduled_start, start_date)
            duration_slots = int(job.effective_duration * (60 // self.slot_minutes))
            end_slot = start_slot + duration_slots
            
            # Mark slots as used
            for s in range(max(0, start_slot), min(total_slots, end_slot)):
                usage_map[(job.printer_id, s)] = str(job.id)
            
            # Update printer state
            state.colors = job.colors_list
            state.job_count += 1
            state.last_item = job.item_name
            state.last_end_slot = end_slot
        
        # Get pending jobs to schedule
        pending_jobs = db.query(Job).filter(
            Job.status == JobStatus.PENDING,
            Job.hold == False
        ).order_by(Job.priority, Job.created_at).all()
        
        # Track last item placement globally (for grouping same items)
        last_item_anywhere: Dict[str, int] = {}  # item_name -> last_end_slot
        
        # Schedule each job
        for job in pending_jobs:
            best_fit = self._find_best_fit(
                job=job,
                printer_states=printer_states,
                usage_map=usage_map,
                start_date=start_date,
                total_slots=total_slots,
                last_item_anywhere=last_item_anywhere,
                lookahead_job=None  # Could add lookahead optimization
            )
            
            if best_fit:
                # Apply the assignment
                self._apply_assignment(
                    job=job,
                    fit=best_fit,
                    printer_states=printer_states,
                    usage_map=usage_map,
                    start_date=start_date,
                    last_item_anywhere=last_item_anywhere
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
        total_slots: int,
        last_item_anywhere: Dict[str, int],
        lookahead_job: Optional[Job] = None
    ) -> Optional[Dict]:
        """
        Find the best printer and time slot for a job.
        
        Returns dict with assignment details or None if no fit found.
        """
        best_fit = None
        best_score = float('-inf')
        
        required_colors = job.colors_list
        duration_slots = int(job.effective_duration * (60 // self.slot_minutes))
        
        # Minimum start slot is "now" (slot 0)
        now_slot = 0
        
        for printer_id, state in printer_states.items():
            color_score = self._calculate_color_score(state.colors, required_colors)
            requires_setup = self._requires_setup(state.colors, required_colors)
            total_slots_needed = duration_slots + (self.config.setup_duration_slots if requires_setup else 0)
            
            # Search for first available window
            for start_slot in range(now_slot, total_slots - total_slots_needed + 1):
                job_start_slot = start_slot + (self.config.setup_duration_slots if requires_setup else 0)
                job_start_time = self._slot_to_time(job_start_slot, start_date)
                
                # Skip blackout times
                if self._is_blackout_time(job_start_time):
                    continue
                
                # Check for conflicts
                conflict = False
                for s in range(start_slot, start_slot + total_slots_needed):
                    if (printer_id, s) in usage_map:
                        conflict = True
                        break
                
                if conflict:
                    continue
                
                # Calculate overall score
                score = color_score
                score -= 30 if requires_setup else 0
                score -= state.job_count * 20  # Spread load across printers
                score -= job.priority * 50  # Higher priority = lower number = better
                
                # Bonus for grouping same items
                if job.item_name in last_item_anywhere:
                    distance = abs(start_slot - last_item_anywhere[job.item_name])
                    if distance == 0:
                        score += 1000
                    elif distance <= 4:
                        score += 300
                    else:
                        score += 100
                
                # Lookahead bonus (if next job matches these colors)
                if lookahead_job:
                    lookahead_score = self._calculate_color_score(required_colors, lookahead_job.colors_list)
                    score += lookahead_score * 0.25
                
                if score > best_score:
                    best_score = score
                    end_slot = job_start_slot + duration_slots
                    best_fit = {
                        "printer_id": printer_id,
                        "printer_name": state.name,
                        "start_slot": start_slot,
                        "job_start_slot": job_start_slot,
                        "end_slot": end_slot,
                        "start_time": job_start_time,
                        "end_time": self._slot_to_time(end_slot, start_date),
                        "requires_setup": requires_setup,
                        "match_score": int((color_score / 100) * 100),  # Normalize to 0-100
                        "score": score
                    }
                
                # First-fit per printer (like original script)
                break
        
        return best_fit
    
    def _apply_assignment(
        self,
        job: Job,
        fit: Dict,
        printer_states: Dict[int, PrinterState],
        usage_map: Dict[Tuple[int, int], str],
        start_date: datetime,
        last_item_anywhere: Dict[str, int]
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
        state.colors = job.colors_list
        state.job_count += 1
        state.last_item = job.item_name
        state.last_end_slot = fit["end_slot"]
        
        # Track for grouping
        last_item_anywhere[job.item_name] = fit["end_slot"]


def run_scheduler(db: Session, config: Optional[SchedulerConfig] = None) -> SchedulerResult:
    """Convenience function to run the scheduler."""
    scheduler = Scheduler(config)
    return scheduler.run(db)
