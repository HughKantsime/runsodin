"""
Contract test — complete_job must be race-safe idempotent.

Guards R5 from the 2026-04-12 Codex adversarial review:
    jobs_lifecycle.py:79-173 had no state guard on complete_job(). A client
    retry after a network timeout, or two operators pressing "complete"
    near-simultaneously, would run spool deductions TWICE and append
    duplicate SpoolUsage rows — permanent inventory corruption.

Verification round (2026-04-12): the first revision used a Python-level
`if job.status == COMPLETED: return` check. That handles serial retries
but is broken under concurrency — two threads can both observe
status!=COMPLETED, both pass the guard, both deduct. This contract now
requires the atomic UPDATE pattern:

    UPDATE jobs SET status='completed', ... WHERE id=:id AND status!='completed'
    if rowcount == 0: return existing job

The DB enforces single-winner semantics, not Python.

Source-level gate so the race-safe pattern cannot be regressed.
Run without container: pytest tests/test_contracts/test_job_complete_idempotency.py -v
"""

import ast
import re
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
JOBS_LIFECYCLE = BACKEND_DIR / "modules" / "jobs" / "routes" / "jobs_lifecycle.py"


def _get_function_source(source: str, name: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node)
    return ""


class TestCompleteJobAtomicTransition:
    """complete_job() must use an atomic UPDATE for the state transition."""

    def test_complete_job_uses_atomic_conditional_update(self):
        source = JOBS_LIFECYCLE.read_text()
        fn_src = _get_function_source(source, "complete_job")
        assert fn_src, "complete_job function is missing from jobs_lifecycle.py"

        # Must reference JobStatus.COMPLETED.
        assert "JobStatus.COMPLETED" in fn_src

        # Look for `update(Job).where(... status != JobStatus.COMPLETED ...)`
        # The exact whitespace can vary, so use a structural regex.
        patterns_required = [
            re.compile(r"update\(\s*Job\s*\)", re.MULTILINE),
            re.compile(r"\.where\([^)]*Job\.status\s*!=\s*JobStatus\.COMPLETED",
                       re.MULTILINE | re.DOTALL),
            re.compile(r"\.values\(", re.MULTILINE),
            re.compile(r"result\.rowcount\s*==\s*0", re.MULTILINE),
        ]
        missing = [p.pattern for p in patterns_required if not p.search(fn_src)]
        assert not missing, (
            "complete_job no longer uses the race-safe atomic-UPDATE pattern. "
            "R5 verification requires:\n"
            "  result = db.execute(update(Job).where(Job.id == :id, "
            "Job.status != JobStatus.COMPLETED).values(...))\n"
            "  if result.rowcount == 0: return job  # already completed\n\n"
            f"Missing pattern(s): {missing}\n\n"
            "Why: the previous Python-level `if job.status == COMPLETED: "
            "return` check is broken under concurrency — two threads can "
            "both pass the guard before either commits."
        )

    def test_no_python_level_status_guard_only(self):
        """A bare `if status == COMPLETED: return` is NOT enough on its own.

        We accept it as a fast path, but it must be paired with the atomic
        UPDATE — which the previous test guarantees.
        """
        source = JOBS_LIFECYCLE.read_text()
        fn_src = _get_function_source(source, "complete_job")

        # The atomic UPDATE itself is the guarantee. Just make sure the
        # function isn't doing direct attribute mutation followed by the
        # deduction loop without the conditional UPDATE protecting it.
        # (Direct `job.status = JobStatus.COMPLETED` outside of update()
        #  values() would defeat the race-safety.)
        bad_pattern = re.compile(
            r"^\s*job\.status\s*=\s*JobStatus\.COMPLETED",
            re.MULTILINE,
        )
        bad_matches = bad_pattern.findall(fn_src)
        assert not bad_matches, (
            "complete_job mutates job.status directly. The transition must "
            "go through the atomic UPDATE (its .values() block sets status). "
            "Direct attribute assignment is NOT race-safe and re-introduces "
            "the double-deduction bug."
        )
