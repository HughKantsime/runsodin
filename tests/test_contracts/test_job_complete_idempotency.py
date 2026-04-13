"""
Contract test — complete_job must be idempotent.

Guards R5 from the 2026-04-12 Codex adversarial review:
    jobs_lifecycle.py:79-173 had no state guard on complete_job(). A client
    retry after a network timeout, or two operators pressing "complete"
    near-simultaneously, would run spool deductions TWICE and append
    duplicate SpoolUsage rows — permanent inventory corruption.

Source-level gate so the idempotency check cannot be silently removed.
The actual runtime behavior is covered by tests/test_e2e/ (DB integration).

Run without container: pytest tests/test_contracts/test_job_complete_idempotency.py -v
"""

import ast
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
JOBS_LIFECYCLE = BACKEND_DIR / "modules" / "jobs" / "routes" / "jobs_lifecycle.py"


def _get_function_source(source: str, name: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node)
    return ""


class TestCompleteJobIdempotency:
    """complete_job() must short-circuit when the job is already completed."""

    def test_complete_job_has_idempotency_guard(self):
        source = JOBS_LIFECYCLE.read_text()
        fn_src = _get_function_source(source, "complete_job")
        assert fn_src, "complete_job function is missing from jobs_lifecycle.py"

        # The guard must check job.status == COMPLETED and return early.
        assert "JobStatus.COMPLETED" in fn_src, (
            "complete_job must reference JobStatus.COMPLETED somewhere — "
            "it needs to check the current status before running mutations."
        )

        # More specifically: the status check must appear BEFORE the
        # mutation section. We check this structurally by finding the
        # index of the guard and comparing it to the first mutation.
        early_check_idx = fn_src.find("if job.status == JobStatus.COMPLETED")
        first_mutation_idx = fn_src.find("job.status = JobStatus.COMPLETED")

        assert early_check_idx > 0, (
            "complete_job does not guard against re-completion. "
            "R5 requires: `if job.status == JobStatus.COMPLETED: return job` "
            "as one of the first statements after loading the job."
        )
        assert early_check_idx < first_mutation_idx, (
            "complete_job's idempotency guard must come BEFORE the status "
            "mutation; otherwise it can never fire. Current order is reversed."
        )

    def test_guard_returns_without_side_effects(self):
        source = JOBS_LIFECYCLE.read_text()
        fn_src = _get_function_source(source, "complete_job")

        # Extract just the guard block. It should be a simple `if .../ return job`
        # without any db.add, db.commit, SpoolUsage, etc.
        guard_start = fn_src.find("if job.status == JobStatus.COMPLETED")
        guard_end = fn_src.find("\n\n", guard_start)
        if guard_end == -1:
            guard_end = guard_start + 200
        guard_block = fn_src[guard_start:guard_end]

        # No mutations should appear inside the guard block
        forbidden = ["db.add(", "db.commit(", "SpoolUsage(", "log_audit(", "slot.color ="]
        violations = [f for f in forbidden if f in guard_block]
        assert not violations, (
            f"complete_job's idempotency guard contains side effects "
            f"({violations}). The guard block must be a pure early-return "
            f"— any mutation here re-enables the double-deduction bug."
        )
