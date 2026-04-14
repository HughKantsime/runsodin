"""
Contract test — complete_job() must push consumption back to Spoolman.

Guards the v1.8.5 Spoolman bidirectional-sync promise. The previous
integration was pull-only; marketing was caveated with "push-back on
roadmap". We've shipped the push; these tests make sure nobody silently
regresses it.

Static-parsing tests (no DB / no network): they inspect the source of
jobs_lifecycle.py + the spoolman.py helper to verify:
  - The helper exists, uses safe_post (SSRF defense), and skips unlinked spools.
  - complete_job() wires deductions through the helper after db.commit().
  - Errors are surfaced loudly (logged + appended to job.notes) — no silent drops.
  - The helper goes through safe_post, never raw httpx.post.

Run without container: pytest tests/test_contracts/test_spoolman_push.py -v
"""

import ast
import re
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
# v1.8.5 refactor: helper moved from routes/spoolman.py to services.py so
# it's reachable from other modules via the `.services import` allowlist
# entry in test_no_cross_module_imports.py.
SPOOLMAN_PATH = BACKEND_DIR / "modules" / "inventory" / "services.py"
JOBS_LIFECYCLE = BACKEND_DIR / "modules" / "jobs" / "routes" / "jobs_lifecycle.py"


def _get_function_source(source: str, name: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node)
    return ""


class TestPushHelperShape:
    """push_consumption_to_spoolman() must exist and be safe."""

    def test_helper_exists(self):
        src = SPOOLMAN_PATH.read_text()
        fn = _get_function_source(src, "push_consumption_to_spoolman")
        assert fn, (
            "push_consumption_to_spoolman is missing from spoolman.py. "
            "v1.8.5 ships bidirectional Spoolman sync — the helper is the "
            "canonical call site."
        )

    def test_helper_uses_safe_post(self):
        src = SPOOLMAN_PATH.read_text()
        fn = _get_function_source(src, "push_consumption_to_spoolman")
        assert "safe_post(" in fn, (
            "push_consumption_to_spoolman must call safe_post() — the "
            "Spoolman URL is user-configured, so SSRF DNS-pin applies. "
            "Raw httpx.post here would re-open the rebinding window "
            "closed under R8."
        )

    def test_helper_does_not_use_raw_httpx(self):
        src = SPOOLMAN_PATH.read_text()
        fn = _get_function_source(src, "push_consumption_to_spoolman")
        # Strip comments + docstrings so we don't catch the word in documentation.
        code_only = re.sub(r'""".*?"""', "", fn, flags=re.DOTALL)
        code_only = re.sub(r"#[^\n]*", "", code_only)
        assert "httpx.post(" not in code_only, (
            "push_consumption_to_spoolman uses raw httpx.post — replace "
            "with safe_post() so SSRF / DNS-rebinding is enforced."
        )

    def test_helper_skips_unlinked_spools(self):
        """When spoolman_spool_id is None, helper must skip without raising
        and without issuing a network call."""
        src = SPOOLMAN_PATH.read_text()
        fn = _get_function_source(src, "push_consumption_to_spoolman")
        # Look for either `if not spoolman_id:` or `if spoolman_id is None:` style
        patterns = [
            r"if\s+not\s+spoolman_id\s*:",
            r"if\s+spoolman_id\s+is\s+None",
            r"if\s+not\s+d\.get\(\s*[\"']spoolman_spool_id[\"']\s*\)\s*:",
        ]
        assert any(re.search(p, fn) for p in patterns), (
            "push_consumption_to_spoolman must explicitly skip when "
            "spoolman_spool_id is None. Unlinked spools are a supported "
            "state — raising or issuing a bogus call breaks jobs whose "
            "deductions include a mix of linked and unlinked spools."
        )

    def test_helper_no_ops_when_disabled(self):
        """Empty / unset settings.spoolman_url → no-op, no error."""
        src = SPOOLMAN_PATH.read_text()
        fn = _get_function_source(src, "push_consumption_to_spoolman")
        assert "settings.spoolman_url" in fn, (
            "Helper must consult settings.spoolman_url to decide whether "
            "to push at all. Pushing to an empty URL would be a bug."
        )


class TestCompleteJobWiring:
    """complete_job() must invoke the push helper."""

    def test_complete_job_calls_push_helper(self):
        src = JOBS_LIFECYCLE.read_text()
        fn = _get_function_source(src, "complete_job")
        assert "push_consumption_to_spoolman" in fn, (
            "complete_job() does not call push_consumption_to_spoolman. "
            "Without the call, Spoolman push-back doesn't ship — despite "
            "marketing saying it does."
        )

    def test_push_runs_in_background_thread(self):
        """v1.8.6 (codex pass 4): push must NOT block the request worker.
        A slow / unreachable Spoolman could hold the worker for
        5s × linked_spools per completion otherwise."""
        src = JOBS_LIFECYCLE.read_text()
        fn = _get_function_source(src, "complete_job")
        assert "threading.Thread" in fn and "daemon=True" in fn, (
            "Spoolman push must fire on a daemon thread so unreachable "
            "Spoolman cannot exhaust request workers. Same pattern as "
            "send_webhook in modules/notifications/channels.py."
        )

    def test_push_after_commit(self):
        """Push must happen AFTER db.commit() so local state persists even
        when Spoolman is unreachable."""
        src = JOBS_LIFECYCLE.read_text()
        fn = _get_function_source(src, "complete_job")
        # Find the SpoolUsage commit line and the push thread spawn;
        # commit must precede the thread.start().
        commit_idx = fn.find("db.commit()")
        spawn_idx = fn.find("threading.Thread")
        assert commit_idx > -1, "db.commit() not found in complete_job"
        assert spawn_idx > -1, "threading.Thread not found in complete_job"
        assert commit_idx < spawn_idx, (
            "Spoolman push thread is spawned BEFORE the local db.commit() — "
            "that means a Spoolman failure could lose the authoritative "
            "local spool state. Spawn must be strictly after commit."
        )

    def test_errors_surface_in_job_notes_sanitized(self):
        """v1.8.6 (codex pass 4): the note that lands in job.notes must be
        SANITIZED — no raw exception text, no Spoolman URL, no internal IPs.
        job.notes is returned in JobResponse, so it's viewable by anyone
        with job access. The previous version copied raw exception strings
        which could leak the configured Spoolman URL or a private IP from
        WebhookSSRFError."""
        src = JOBS_LIFECYCLE.read_text()
        fn = _get_function_source(src, "complete_job")
        assert "Spoolman push failed" in fn, (
            "Push errors must surface as 'Spoolman push failed' in job.notes "
            "so operators see them in the UI. Logs alone are too quiet."
        )

    def test_push_errors_logged_at_error(self):
        src = JOBS_LIFECYCLE.read_text()
        fn = _get_function_source(src, "complete_job")
        assert "log.error" in fn and "Spoolman" in fn, (
            "Push errors must log at ERROR. Lower levels make the failure "
            "invisible in the standard logging pipeline."
        )

    def test_job_notes_does_not_leak_exception_text(self):
        """Codex pass 4 sanitization: the note appended to job.notes must
        NOT contain raw exception variables, error string interpolation,
        or anything that could leak the configured Spoolman URL / private
        IPs from WebhookSSRFError. job.notes is returned in JobResponse —
        any viewer can read it."""
        src = JOBS_LIFECYCLE.read_text()
        fn = _get_function_source(src, "complete_job")
        # Strip docstrings so we don't catch the doc text.
        import re as _re
        code_only = _re.sub(r'""".*?"""', "", fn, flags=_re.DOTALL)
        # Forbidden: f-strings or .format() that interpolate exception
        # variables or push-error strings into the marker that goes into
        # job.notes. Markers must be static strings.
        # We allow the "see server logs" pattern; we forbid {e} or {err} in
        # any line that ends up assigned to a notes-marker variable.
        forbidden_patterns = [
            r"marker\s*=.*\{e[\s)\}]",   # marker = f"...{e}..." or {err}
            r"marker\s*=.*\{err[\s\}]",
            r":m.*:\s*f\".*\{e\}",       # in a params dict
        ]
        for pat in forbidden_patterns:
            assert not _re.search(pat, code_only), (
                f"Sanitization regression: marker text interpolates an "
                f"exception variable ({pat}). Keep raw exception text in "
                f"server logs only — job.notes is customer-visible."
            )

    def test_deduction_carries_spoolman_id(self):
        """The deduction dict must include spoolman_spool_id so the helper
        can route pushes to the right Spoolman row."""
        src = JOBS_LIFECYCLE.read_text()
        fn = _get_function_source(src, "complete_job")
        assert '"spoolman_spool_id":' in fn or "'spoolman_spool_id':" in fn, (
            "deductions entry missing spoolman_spool_id. The helper can't "
            "route pushes without it."
        )
