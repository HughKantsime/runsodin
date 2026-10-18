"""The IP allowlist policy is cached in process for a short TTL and fails closed.

The middleware read the policy on every /api request (one database connection each).
It now reads at most once per TTL per worker and database; the saving worker invalidates
at once, other workers apply a change within the TTL, and an unreadable policy denies."""
import json

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def database(tmp_path, monkeypatch):
    from core import ip_allowlist_cache
    ip_allowlist_cache.invalidate()
    monkeypatch.delenv("ODIN_IP_ALLOWLIST_CACHE_SECONDS", raising=False)
    engine = create_engine(f"sqlite:///{tmp_path / 'allow.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE system_config (key TEXT PRIMARY KEY, value TEXT)"))
    reads = []

    def factory():
        session = Session(engine)
        real = session.execute

        def execute(statement, *args, **kwargs):
            reads.append(str(statement))
            return real(statement, *args, **kwargs)
        session.execute = execute
        return session
    yield engine, factory, reads
    ip_allowlist_cache.invalidate()
    engine.dispose()


def _store(engine, policy):
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO system_config (key, value) VALUES ('ip_allowlist', :v) "
                                "ON CONFLICT(key) DO UPDATE SET value = :v"), {"v": json.dumps(policy)})


def test_reads_within_the_ttl_reuse_the_cached_policy(database):
    from core.ip_allowlist_cache import policy
    engine, factory, reads = database
    clock = Clock()
    _store(engine, {"enabled": True, "cidrs": ["203.0.113.0/24"]})
    assert policy(factory, clock=clock)["cidrs"] == ["203.0.113.0/24"]
    clock.now += 1.9
    assert policy(factory, clock=clock)["cidrs"] == ["203.0.113.0/24"]
    assert len(reads) == 1


def test_another_worker_applies_a_change_within_the_ttl(database):
    from core import ip_allowlist_cache
    engine, factory, reads = database
    clock = Clock()
    _store(engine, {"enabled": False, "cidrs": []})
    assert ip_allowlist_cache.policy(factory, clock=clock)["enabled"] is False
    _store(engine, {"enabled": True, "cidrs": ["198.51.100.0/24"]})  # written by another worker
    clock.now += 1.0
    assert ip_allowlist_cache.policy(factory, clock=clock)["enabled"] is False  # still inside the 2 s bound
    clock.now += 1.0
    assert ip_allowlist_cache.policy(factory, clock=clock)["enabled"] is True  # applied once the TTL passed


def test_the_saving_worker_applies_its_change_at_once(database):
    from core import ip_allowlist_cache
    engine, factory, reads = database
    clock = Clock()
    assert ip_allowlist_cache.policy(factory, clock=clock) is None
    _store(engine, {"enabled": True, "cidrs": ["198.51.100.0/24"]})
    ip_allowlist_cache.invalidate()
    assert ip_allowlist_cache.policy(factory, clock=clock)["enabled"] is True


def test_an_unreadable_policy_fails_closed_and_is_never_served_stale(database):
    from core import ip_allowlist_cache
    from core.app import _ip_allowlist_denial
    engine, factory, reads = database
    _store(engine, {"enabled": True, "cidrs": ["198.51.100.0/24"]})

    def broken():
        raise RuntimeError("database unavailable")
    assert _ip_allowlist_denial(broken, "203.0.113.9") == 503  # first load fails: denied
    clock = Clock()
    assert ip_allowlist_cache.policy(factory, clock=clock)["enabled"] is True
    clock.now += 5
    with pytest.raises(RuntimeError):
        ip_allowlist_cache.policy(broken, clock=clock)  # expired entry: reload fails, nothing served


def test_ttl_is_configurable_and_bounded(database, monkeypatch):
    from core import ip_allowlist_cache
    engine, factory, reads = database
    monkeypatch.setenv("ODIN_IP_ALLOWLIST_CACHE_SECONDS", "0")
    ip_allowlist_cache.policy(factory)
    ip_allowlist_cache.policy(factory)
    assert len(reads) == 2  # 0 disables the cache
    monkeypatch.setenv("ODIN_IP_ALLOWLIST_CACHE_SECONDS", "31")
    with pytest.raises(ValueError):
        ip_allowlist_cache.cache_ttl_seconds()


def test_the_middleware_check_uses_the_cache(database):
    from core.app import _ip_allowlist_denial
    engine, factory, reads = database
    _store(engine, {"enabled": True, "cidrs": ["198.51.100.0/24"]})
    assert _ip_allowlist_denial(factory, "203.0.113.9") == 403
    assert _ip_allowlist_denial(factory, "198.51.100.4") is None
    assert len(reads) == 1


def test_saving_the_allowlist_invalidates_this_workers_cache():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[2] / "backend/modules/system/routes_config.py").read_text()
    body = source[source.index("async def set_ip_allowlist"):source.index("# ============== Retention Config")]
    assert body.index("db.commit()") < body.index("invalidate()")
