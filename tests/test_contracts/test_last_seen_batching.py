"""
Functional test — last_seen_at writes must be batched.

Guards R6 from the 2026-04-12 Codex adversarial review:
    get_current_user did DB write + commit on every authenticated request
    to update active_sessions.last_seen_at. On SQLite, every dashboard
    poll turned auth into a writer, serializing concurrent reads behind
    the single writer lock and producing SQLITE_BUSY errors on otherwise
    read-only paths.

Fix: in-process cache of "last write time per jti", with a 5-minute
minimum interval between writes. This test exercises the cache logic
directly — it does not require a running DB.

Run without container: pytest tests/test_contracts/test_last_seen_batching.py -v
"""

import time
from unittest.mock import patch

# sys.path and JWT_SECRET_KEY are handled by tests/test_contracts/conftest.py


class TestLastSeenCache:
    """The _should_write_last_seen helper must throttle per-jti writes."""

    def setup_method(self):
        # Clear cache between tests
        from core import dependencies as deps
        deps._last_seen_cache.clear()

    def test_first_call_writes(self):
        from core.dependencies import _should_write_last_seen
        assert _should_write_last_seen("jti-first") is True

    def test_second_call_within_window_skips(self):
        from core.dependencies import _should_write_last_seen
        assert _should_write_last_seen("jti-skip") is True
        # Immediately again — should skip
        assert _should_write_last_seen("jti-skip") is False
        # Many times — still skip
        for _ in range(100):
            assert _should_write_last_seen("jti-skip") is False

    def test_different_jtis_independent(self):
        from core.dependencies import _should_write_last_seen
        assert _should_write_last_seen("jti-a") is True
        # Different jti — should still write
        assert _should_write_last_seen("jti-b") is True
        # Repeats skip
        assert _should_write_last_seen("jti-a") is False
        assert _should_write_last_seen("jti-b") is False

    def test_empty_jti_never_writes(self):
        """Empty jti means we couldn't track it anyway — don't corrupt the cache."""
        from core.dependencies import _should_write_last_seen
        assert _should_write_last_seen("") is False
        assert _should_write_last_seen(None) is False

    def test_cache_expiry_allows_rewrite(self):
        """After the interval passes, a write should be allowed again."""
        from core import dependencies as deps
        from core.dependencies import _should_write_last_seen

        assert _should_write_last_seen("jti-expire") is True
        assert _should_write_last_seen("jti-expire") is False

        # Fake the clock forward by more than the interval
        original_interval = deps._LAST_SEEN_MIN_INTERVAL_SECONDS
        with patch.object(deps, "_LAST_SEEN_MIN_INTERVAL_SECONDS", 0.01):
            # Manually push the cache timestamp into the past
            deps._last_seen_cache["jti-expire"] = time.time() - 1.0
            assert _should_write_last_seen("jti-expire") is True

    def test_forget_last_seen_clears(self):
        """Logout should drop the cache entry so the next login re-writes immediately."""
        from core.dependencies import _should_write_last_seen, _forget_last_seen
        _should_write_last_seen("jti-logout")  # first: writes, caches
        assert _should_write_last_seen("jti-logout") is False  # cached
        _forget_last_seen("jti-logout")
        assert _should_write_last_seen("jti-logout") is True  # fresh again

    def test_thread_safety_two_callers_one_write(self):
        """Two concurrent first-time callers of the same jti — at most one
        should return True within the interval window.

        The lock is on the decision, not the DB. So if two threads race
        with a fresh cache entry, exactly one wins and the other sees
        the cache already populated.
        """
        import threading

        from core import dependencies as deps
        from core.dependencies import _should_write_last_seen

        deps._last_seen_cache.pop("jti-race", None)

        results = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            results.append(_should_write_last_seen("jti-race"))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly one thread should have decided to write; all others skip.
        assert results.count(True) == 1, (
            f"Expected exactly 1 writer, got {results.count(True)} out of 8. "
            f"Lock is broken — concurrent callers will thundering-herd the DB."
        )
        assert results.count(False) == 7


class TestGetCurrentUserUsesCache:
    """Source-level — the DB writes must be gated by _should_write_last_seen."""

    def test_cookie_path_uses_cache_gate(self):
        import ast
        from pathlib import Path

        source = Path(__file__).resolve().parents[2] / "backend" / "core" / "dependencies.py"
        src_text = source.read_text()

        tree = ast.parse(src_text)
        fn_src = ""
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "get_current_user":
                fn_src = ast.get_source_segment(src_text, node)
                break
        assert fn_src, "get_current_user is missing"

        # The UPDATE active_sessions SET last_seen_at statements should be
        # conditional on _should_write_last_seen
        update_count = fn_src.count("UPDATE active_sessions SET last_seen_at")
        gate_count = fn_src.count("_should_write_last_seen(jti)")
        assert update_count == gate_count and update_count >= 2, (
            f"Expected every last_seen UPDATE to be gated by "
            f"_should_write_last_seen. Found {update_count} UPDATEs and "
            f"{gate_count} gates. If any UPDATE runs unconditionally, "
            f"SQLite will thrash under concurrent reads."
        )
