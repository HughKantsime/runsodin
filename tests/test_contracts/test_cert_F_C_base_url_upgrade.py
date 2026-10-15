"""Security review LOW-F/LOW-G and the setup-race 500: a stored public-http base_url
survives upgrade (reset keeps working) but raises an admin health warning; new writes
must be https; the shared loopback helper admits loopback; a lock-timeout maps to 409."""
from __future__ import annotations

import pytest
from core.application_url import validate_base_url, base_url_is_insecure_public
from core.net_identity import is_loopback


def test_validate_rejects_public_http_on_write_but_accepts_on_read():
    with pytest.raises(ValueError):
        validate_base_url("http://odin.example.test")              # new write
    assert validate_base_url("http://odin.example.test", enforce_https=False) == "http://odin.example.test"
    assert validate_base_url("http://192.168.1.5:8000") == "http://192.168.1.5:8000"   # LAN http ok on write
    assert validate_base_url("https://odin.example.test") == "https://odin.example.test"


@pytest.mark.parametrize("url,insecure", [
    ("http://odin.example.test", True), ("http://93.184.216.34:8000", True),
    ("https://odin.example.test", False), ("http://192.168.1.5:8000", False),
    ("http://localhost:8000", False), ("http://printfarm.local", False), ("", False),
])
def test_insecure_public_detection(url, insecure):
    assert base_url_is_insecure_public(url) is insecure


@pytest.mark.parametrize("ip,loop", [
    ("127.0.0.1", True), ("::1", True), ("127.5.5.5", True), ("localhost", True),
    ("10.0.0.1", False), ("::ffff:127.0.0.1", True), ("not-an-ip", False), (None, False), ("", False),
])
def test_shared_loopback(ip, loop):
    assert is_loopback(ip) is loop


def test_stored_public_http_is_reported_as_an_admin_health_warning(tmp_path):
    from pathlib import Path
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session
    from core.schema import bootstrap_database
    from core.application_url import base_url_health_warning
    engine = create_engine("sqlite:///" + str(tmp_path / "h.db"), connect_args={"check_same_thread": False})
    bootstrap_database(engine, Path(__file__).resolve().parents[2] / "backend")
    with engine.begin() as c:
        c.execute(text("INSERT INTO system_config(key, value) VALUES('base_url', :v)"), {"v": '"http://odin.example.test"'})
    with Session(engine) as db:
        assert "unencrypted" in base_url_health_warning(db)
        db.execute(text("UPDATE system_config SET value=:v WHERE key='base_url'"), {"v": '"https://odin.example.test"'})
        assert base_url_health_warning(db) is None
