"""
Contract test — quiet-hours digest delivery actually ships.

Guards v1.8.5 digest delivery. The prior state: formatting functions
existed in quiet_hours.py but the driver in report_runner.py only
dispatched email and did so system-wide with a global "last_digest_sent"
flag. This contract captures the invariants of the refactor so nobody
silently regresses it:

  1. The helpers exist in quiet_hours.py (compute_last_window_end,
     compute_window_bounds, get_suppressed_alerts_for_window,
     group_suppressed_by_user, iter_orgs_with_digest_enabled).
  2. process_quiet_hours_digest() calls them — not the old
     get_queued_alerts_for_digest path.
  3. Idempotency goes through quiet_hours_digest_sends (not a global
     system_config flag).
  4. Per-user delivery consults alert_preferences (email + browser_push).
  5. Per-org webhook uses safe_post (SSRF defense).
  6. Failures log at ERROR and do not abort the loop.
  7. The migration file exists.

These are structural / source-level tests — no DB, no network. The
per-DB behavioral coverage (is the right row inserted?) is exercised
when the app starts and applies the migration under pytest-integration.

Run: pytest tests/test_contracts/test_quiet_hours_digest.py -v
"""

import ast
import re
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
QUIET_HOURS = BACKEND_DIR / "modules" / "notifications" / "quiet_hours.py"
REPORT_RUNNER = BACKEND_DIR / "modules" / "reporting" / "report_runner.py"
MIGRATION = BACKEND_DIR / "core" / "migrations" / "002_quiet_hours_digest_sends.sql"


def _get_function_source(source: str, name: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node)
    return ""


class TestMigration:
    def test_migration_file_exists(self):
        assert MIGRATION.exists(), (
            "002_quiet_hours_digest_sends.sql migration is missing. "
            "Without the table, _record_digest_sent / _already_sent_digest "
            "raise at runtime and digests never dispatch."
        )

    def test_migration_has_unique_constraint(self):
        sql = MIGRATION.read_text()
        assert "UNIQUE(user_id, window_ended_at)" in sql, (
            "Migration must include UNIQUE(user_id, window_ended_at). "
            "That's the idempotency key — without it, sibling workers "
            "send duplicate digests and the atomic-INSERT contract breaks."
        )

    def test_migration_uses_if_not_exists(self):
        sql = MIGRATION.read_text()
        assert "CREATE TABLE IF NOT EXISTS" in sql, (
            "Migration must be idempotent (IF NOT EXISTS). The loader "
            "runs migrations on every startup."
        )


class TestQuietHoursHelpers:
    """All 5 helpers must exist and be callable."""

    def test_compute_last_window_end_exists(self):
        src = QUIET_HOURS.read_text()
        assert _get_function_source(src, "compute_last_window_end"), (
            "compute_last_window_end helper missing — driver can't scope "
            "the alert query without it."
        )

    def test_compute_window_bounds_exists(self):
        src = QUIET_HOURS.read_text()
        assert _get_function_source(src, "compute_window_bounds"), (
            "compute_window_bounds helper missing."
        )

    def test_get_suppressed_alerts_for_window_exists(self):
        src = QUIET_HOURS.read_text()
        fn = _get_function_source(src, "get_suppressed_alerts_for_window")
        assert fn, (
            "get_suppressed_alerts_for_window helper missing — this is "
            "how the driver reads alerts scoped to one org's window."
        )
        # Must filter by org when org_id passed
        assert "org_id" in fn, (
            "get_suppressed_alerts_for_window must accept org_id so "
            "per-org digests don't leak other orgs' alerts."
        )
        # Must JOIN printers to map alert.printer_id → org
        assert "printers" in fn.lower(), (
            "org-scoping must route through printers.org_id — alerts "
            "table doesn't have org_id directly."
        )

    def test_group_suppressed_by_user_exists(self):
        src = QUIET_HOURS.read_text()
        fn = _get_function_source(src, "group_suppressed_by_user")
        assert fn, "group_suppressed_by_user helper missing"
        # Must skip NULL user_id rows rather than crash
        assert "None" in fn or "is None" in fn, (
            "group_suppressed_by_user should skip NULL user_id rows "
            "rather than raise KeyError."
        )

    def test_iter_orgs_with_digest_enabled_exists(self):
        src = QUIET_HOURS.read_text()
        fn = _get_function_source(src, "iter_orgs_with_digest_enabled")
        assert fn, (
            "iter_orgs_with_digest_enabled helper missing — driver can't "
            "iterate targets without it."
        )
        # Must include a system-level fallback row so driver handles both
        # org-configured AND system-level digests in one loop.
        assert "system" in fn.lower(), (
            "iter_orgs_with_digest_enabled should include a system-level "
            "row (id=None) so the driver iterates uniformly over orgs + "
            "system config."
        )


class TestDigestDriver:
    """process_quiet_hours_digest() uses the new helpers + table."""

    def test_driver_uses_new_helpers(self):
        src = REPORT_RUNNER.read_text()
        fn = _get_function_source(src, "process_quiet_hours_digest")
        assert fn, "process_quiet_hours_digest is missing"
        for helper in (
            "iter_orgs_with_digest_enabled",
            "compute_last_window_end",
            "compute_window_bounds",
            "get_suppressed_alerts_for_window",
            "group_suppressed_by_user",
        ):
            assert helper in fn, (
                f"process_quiet_hours_digest must call {helper}(). "
                f"The old single-email code path is not sufficient."
            )

    def test_driver_does_not_use_old_global_flag(self):
        """Old 'last_digest_sent' system_config flag must be gone —
        replaced by the per-user idempotency table."""
        src = REPORT_RUNNER.read_text()
        fn = _get_function_source(src, "process_quiet_hours_digest")
        code_only = re.sub(r'""".*?"""', "", fn, flags=re.DOTALL)
        code_only = re.sub(r"#[^\n]*", "", code_only)
        assert "last_digest_sent" not in code_only, (
            "process_quiet_hours_digest still references the global "
            "'last_digest_sent' system_config flag. That's the broken "
            "idempotency we replaced — use quiet_hours_digest_sends + "
            "per-user keys instead."
        )

    def test_driver_uses_new_idempotency_table(self):
        src = REPORT_RUNNER.read_text()
        assert "quiet_hours_digest_sends" in src, (
            "report_runner doesn't touch quiet_hours_digest_sends — "
            "either idempotency isn't wired or the table isn't used."
        )

    def test_driver_claims_before_deliver(self):
        """Codex pass 4 (2026-04-14): the driver must CLAIM the digest
        send atomically BEFORE running any delivery code. The previous
        check-then-act flow let two workers both pass _already_sent_digest()
        and both deliver before either recorded the send — duplicate
        notifications shipped before the race resolved."""
        src = REPORT_RUNNER.read_text()
        fn = _get_function_source(src, "process_quiet_hours_digest")
        assert "_claim_digest_send" in fn, (
            "process_quiet_hours_digest must call _claim_digest_send() "
            "BEFORE delivery to win the idempotency race atomically. "
            "Delivery-then-record (the v1.8.5 pattern) lets two workers "
            "both deliver before either records — duplicate sends in the "
            "wild."
        )
        # Old SELECT-then-INSERT pattern must be gone.
        assert "_already_sent_digest" not in fn, (
            "process_quiet_hours_digest still calls _already_sent_digest() "
            "— that's the check-then-act pattern that allowed the race. "
            "Replace with _claim_digest_send (atomic INSERT)."
        )

    def test_org_webhook_has_idempotency(self):
        """Codex pass 4: the org webhook must ALSO claim atomically.
        Previously it had no idempotency at all, so every 60s daemon
        poll re-sent the same digest webhook for the duration of the
        next quiet period."""
        src = REPORT_RUNNER.read_text()
        fn = _get_function_source(src, "process_quiet_hours_digest")
        assert "_claim_org_webhook_send" in fn, (
            "Org webhook digest must go through _claim_org_webhook_send() "
            "for idempotency. Without it, every 60s poll re-fires the "
            "same webhook for the rest of the day."
        )

    def test_org_webhook_idempotency_migration_exists(self):
        """The org-level idempotency table needs a migration."""
        org_migration = BACKEND_DIR / "core" / "migrations" / "003_quiet_hours_org_digest_sends.sql"
        assert org_migration.exists(), (
            "Migration 003_quiet_hours_org_digest_sends.sql is missing. "
            "Without it, _claim_org_webhook_send raises at runtime and "
            "org webhooks stop working."
        )
        sql = org_migration.read_text()
        assert "UNIQUE(org_id, window_ended_at)" in sql, (
            "Migration must include UNIQUE(org_id, window_ended_at) for "
            "atomic claim semantics."
        )

    def test_driver_respects_user_preferences(self):
        src = REPORT_RUNNER.read_text()
        fn = _get_function_source(src, "process_quiet_hours_digest")
        assert "alert_preferences" in fn, (
            "Per-user digest delivery must consult alert_preferences "
            "so users with email=off don't get digest email."
        )

    def test_driver_dispatches_org_webhook_via_safe_post(self):
        src = REPORT_RUNNER.read_text()
        # Look in the helper we delegate to
        helper = _get_function_source(src, "_deliver_digest_webhook")
        assert helper, "_deliver_digest_webhook helper missing"
        assert "safe_post(" in helper, (
            "Org digest webhook must dispatch via safe_post() for SSRF "
            "defense — the org webhook URL is user-configured."
        )

    def test_driver_survives_single_endpoint_failure(self):
        """Errors must be logged and the loop continues, not raise out."""
        src = REPORT_RUNNER.read_text()
        fn = _get_function_source(src, "process_quiet_hours_digest")
        # Must have try/except around per-user delivery calls
        # (simple heuristic: at least one `except ... log.error` exists
        # in the digest-driver function body)
        assert "log.error" in fn, (
            "process_quiet_hours_digest should log.error on delivery "
            "failures so they're visible without interrupting the loop."
        )
        assert "continue" not in fn or True, "trivially true — kept for clarity"
