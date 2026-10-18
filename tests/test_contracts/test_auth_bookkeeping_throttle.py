"""Authenticated reads do not write unless the bookkeeping they keep is stale.

active_sessions.last_seen_at and api_tokens.last_used_at are refreshed at most once
per ODIN_SESSION_LAST_SEEN_INTERVAL_SECONDS (default 60) per row, judged against
the stored value, so a fresh row costs a read and no SQLite write lock -- across
API workers, not only within one. The UPDATE itself is conditional so racing
workers cannot both rewrite. GET /quotas reads usage without creating its row.

Consumers of these values and why the granularity is acceptable for each:
* GET /sessions and /admin/sessions show "Last seen" and order by it: a minute is
  finer than the 5-minute in-process batching this replaces.
* The privacy export reports the stored value as it is.
* The API token list shows the last-used date (day granularity).
* No idle timeout, revocation or expiry reads either column (revocation is the
  token blacklist; expiry is the JWT/token expires_at) -- asserted below.
"""
import asyncio
import importlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _ROOT / "backend"


@pytest.fixture
def auth_db(tmp_path, monkeypatch):
    from core import dependencies
    from core.schema import bootstrap_database
    engine = create_engine(f"sqlite:///{tmp_path / 'auth.db'}")
    bootstrap_database(engine, _BACKEND)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO users (id,username,email,password_hash,role,is_active) "
                                "VALUES (1,'admin','a@x.test','fixture','admin',1)"))
    monkeypatch.setattr(dependencies, "_last_seen_cache", {})
    writes = []

    def record(conn, cursor, statement, *args):
        if statement.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")):
            writes.append(" ".join(statement.split()))
    event.listen(engine, "before_cursor_execute", record)
    return engine, writes


def _session(engine, jti, last_seen):
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO active_sessions (user_id, token_jti, last_seen_at) VALUES (1, :jti, :seen)"),
                           {"jti": jti, "seen": last_seen})


def _stored(engine, jti):
    from core.db_compat import as_utc_datetime
    with engine.connect() as connection:
        value = connection.execute(text("SELECT last_seen_at FROM active_sessions WHERE token_jti=:jti"), {"jti": jti}).scalar_one()
    return None if value is None else as_utc_datetime(value)


def _authenticate(engine, jti):
    import jwt
    import core.auth as auth
    from core.dependencies import get_current_user
    token = jwt.encode({"sub": "admin", "role": "admin", "jti": jti, "iat": datetime.now(timezone.utc),
                        "exp": datetime.now(timezone.utc) + timedelta(hours=1)}, auth.SECRET_KEY, algorithm=auth.ALGORITHM)

    class Request:
        cookies = {}
        headers = {}
        url = type("U", (), {"path": "/api/x"})()
        method = "GET"
        scope = {}

    async def resolve():
        with Session(engine) as db:
            return await get_current_user(Request(), token=token, db=db)
    return asyncio.run(resolve())


def _last_seen_writes(writes):
    return [w for w in writes if w.startswith("UPDATE active_sessions SET last_seen_at")]


def test_fresh_session_costs_no_write(auth_db):
    engine, writes = auth_db
    seen = datetime.now(timezone.utc) - timedelta(seconds=20)
    _session(engine, "fresh", seen)
    writes.clear()
    assert _authenticate(engine, "fresh")
    assert _last_seen_writes(writes) == [] and not [w for w in writes if "active_sessions" in w]
    assert abs((_stored(engine, "fresh") - seen).total_seconds()) < 1


@pytest.mark.parametrize("age", [None, 61, 3600])
def test_stale_or_unset_session_is_refreshed_once(auth_db, age):
    engine, writes = auth_db
    _session(engine, "stale", None if age is None else datetime.now(timezone.utc) - timedelta(seconds=age))
    writes.clear()
    assert _authenticate(engine, "stale")
    refreshed = _stored(engine, "stale")
    assert refreshed and (datetime.now(timezone.utc) - refreshed).total_seconds() < 5
    assert len(_last_seen_writes(writes)) == 1
    assert "last_seen_at IS NULL OR" in _last_seen_writes(writes)[0]  # race-safe conditional


def test_other_worker_refresh_suppresses_this_workers_write(auth_db):
    """The stored value, not this process's memory, decides: a row another worker
    refreshed a moment ago is not rewritten even though this process never wrote it."""
    engine, writes = auth_db
    _session(engine, "shared", datetime.now(timezone.utc) - timedelta(seconds=5))
    writes.clear()
    assert _authenticate(engine, "shared")
    assert _last_seen_writes(writes) == []


def test_conditional_update_never_moves_a_newer_value_back(auth_db):
    from core import dependencies
    engine, _ = auth_db
    newer = datetime.now(timezone.utc)
    _session(engine, "raced", newer)
    with Session(engine) as db:
        assert dependencies._touch_session_last_seen(db, "raced", now=newer - timedelta(seconds=90)) is False
    assert abs((_stored(engine, "raced") - newer).total_seconds()) < 1


def test_interval_is_configurable_and_validated(monkeypatch):
    from core import dependencies
    assert dependencies._last_seen_interval_from_env({}) == 60
    assert dependencies._last_seen_interval_from_env({"ODIN_SESSION_LAST_SEEN_INTERVAL_SECONDS": "300"}) == 300
    for invalid in ("0", "-5", "abc", "1.5", "86401"):
        with pytest.raises(ValueError):
            dependencies._last_seen_interval_from_env({"ODIN_SESSION_LAST_SEEN_INTERVAL_SECONDS": invalid})


def test_configured_interval_governs_the_write(auth_db, monkeypatch):
    from core import dependencies
    engine, writes = auth_db
    monkeypatch.setattr(dependencies, "_LAST_SEEN_MIN_INTERVAL_SECONDS", 10)
    _session(engine, "short", datetime.now(timezone.utc) - timedelta(seconds=20))
    writes.clear()
    assert _authenticate(engine, "short")
    assert len(_last_seen_writes(writes)) == 1


def test_api_token_last_used_is_throttled_the_same_way(auth_db):
    from core.auth import hash_password
    from core.dependencies import resolve_user_api_token
    engine, writes = auth_db
    key = "odin_synthk_" + "t" * 24
    recent = datetime.now(timezone.utc) - timedelta(seconds=10)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO api_tokens (id,user_id,name,token_hash,token_prefix,scopes,last_used_at) "
                                "VALUES (5,1,'t',:hash,:prefix,'[\"read\"]',:used)"),
                           {"hash": hash_password(key), "prefix": key[:10], "used": recent})
    writes.clear()
    with Session(engine) as db:
        assert resolve_user_api_token(key, db)
    assert not [w for w in writes if "api_tokens" in w]
    with engine.begin() as connection:
        connection.execute(text("UPDATE api_tokens SET last_used_at=:used WHERE id=5"),
                           {"used": datetime.now(timezone.utc) - timedelta(minutes=5)})
    writes.clear()
    with Session(engine) as db:
        assert resolve_user_api_token(key, db)
    token_writes = [w for w in writes if "api_tokens" in w]
    assert len(token_writes) == 1 and "last_used_at IS NULL OR" in token_writes[0]


def test_quota_read_does_not_create_the_usage_row(auth_db, monkeypatch):
    from modules.organizations import routes_sessions
    engine, writes = auth_db
    monkeypatch.setattr(routes_sessions, "require_feature", lambda feature: None)
    writes.clear()
    with Session(engine) as db:
        body = asyncio.run(routes_sessions.get_my_quota(current_user={"id": 1, "quota_period": "monthly"}, db=db))
    assert body["usage"] == {"grams_used": 0, "hours_used": 0, "jobs_used": 0}
    from core.quota import _get_period_key
    assert body["period_key"] == _get_period_key("monthly")
    assert writes == []


def test_session_list_reflects_a_refresh_and_orders_by_it(auth_db):
    """GET /sessions: a session used after the interval moves to the top with its new time."""
    from modules.organizations import routes_sessions
    engine, _ = auth_db
    _session(engine, "older-used", datetime.now(timezone.utc) - timedelta(hours=2))
    _session(engine, "recent", datetime.now(timezone.utc) - timedelta(minutes=30))
    assert _authenticate(engine, "older-used")

    class Request:
        cookies = {}
        headers = {}

    with Session(engine) as db:
        listed = asyncio.run(routes_sessions.list_sessions(Request(), current_user={"id": 1}, db=db))
    from core.db_compat import as_utc_datetime
    assert len(listed) == 2
    first = as_utc_datetime(listed[0]["last_seen_at"])
    assert (datetime.now(timezone.utc) - first).total_seconds() < 5  # the refreshed one, first


def test_only_the_retention_cleanup_compares_bookkeeping_times():
    """Every time comparison on these columns is known: the throttle itself and the
    retention cleanup that drops sessions idle for 48 h. A new one (an idle timeout,
    say) must account for the interval and be added here deliberately."""
    comparisons = set()
    pattern = re.compile(r"last_(seen|used)_at\s*(<|>|<=|>=)\s*[:?]|[<>]=?\s*\w*\.?last_(seen|used)_at")
    for file in list((_BACKEND / "core").rglob("*.py")) + list((_BACKEND / "modules").rglob("*.py")):
        if "preserved02" in str(file):
            continue
        for line in file.read_text(errors="ignore").splitlines():
            if pattern.search(line) and not line.strip().startswith("#"):
                comparisons.add(str(file.relative_to(_BACKEND)))
    assert comparisons == {"core/dependencies.py", "modules/system/routes_config.py"}, comparisons
    from core import dependencies
    # Worst-case staleness (the maximum interval) stays inside the 48 h idle cutoff.
    assert dependencies._MAX_LAST_SEEN_INTERVAL_SECONDS < 48 * 3600


def test_retention_cleanup_keeps_a_session_used_within_48_hours(auth_db):
    """A session refreshed by use is kept; one idle past 48 h is removed."""
    from modules.system import routes_config
    engine, _ = auth_db
    _session(engine, "in-use", datetime.now(timezone.utc) - timedelta(hours=60))
    _session(engine, "abandoned", datetime.now(timezone.utc) - timedelta(hours=60))
    assert _authenticate(engine, "in-use")  # stale -> refreshed now
    with Session(engine) as db:
        result = asyncio.run(routes_config.run_retention_cleanup(current_user={"id": 1, "role": "admin", "group_id": None}, db=db))
    assert result["status"] in ("ok", "partial")
    with engine.connect() as connection:
        remaining = {row[0] for row in connection.execute(text("SELECT token_jti FROM active_sessions"))}
    assert remaining == {"in-use"}
