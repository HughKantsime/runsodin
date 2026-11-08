"""Alert history and optional session bookkeeping must not sign a user out."""
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from starlette.testclient import TestClient


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    from core.schema import bootstrap_database

    engine = create_engine(f"sqlite:///{tmp_path_factory.mktemp('alerts401') / 'test.db'}")
    bootstrap_database(engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) "
                          "VALUES(1,'alerts-test','unused','admin',1),(2,'other','unused','viewer',1)"))
    yield engine
    engine.dispose()


@pytest.fixture
def backend(engine, monkeypatch):
    from core import auth, db, dependencies
    from core.app import _register_http_middleware
    from core.config import settings
    from modules.notifications.routes.alerts import router

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM alerts"))
        conn.execute(text("DELETE FROM printers"))
        conn.execute(text("DELETE FROM token_blacklist"))
        conn.execute(text("UPDATE users SET is_active=1 WHERE id=1"))
    state = SimpleNamespace(rollbacks=0, fail_lookup=None, route_calls=0)

    class TestSession(Session):
        def __init__(self):
            super().__init__(bind=engine)

        def execute(self, statement, *args, **kwargs):
            if state.fail_lookup and state.fail_lookup in str(statement):
                raise RuntimeError("auth database unavailable")
            return super().execute(statement, *args, **kwargs)

        def rollback(self):
            state.rollbacks += 1
            return super().rollback()

    def get_db():
        with TestSession() as session:
            yield session

    monkeypatch.setattr(settings, "api_key", "test-perimeter")
    monkeypatch.setattr(db, "SessionLocal", TestSession)
    monkeypatch.setattr("core.app._ip_allowlist_denial", lambda *_: None)
    monkeypatch.setattr(dependencies, "_last_seen_cache", {})
    app = FastAPI()
    app.dependency_overrides[db.get_db] = get_db
    # Exercise the real JWT resolver; unrelated controlled-request policy is
    # outside these contracts. Alert RBAC and response redaction remain real.
    app.dependency_overrides[dependencies.get_current_user] = dependencies._resolve_current_user
    app.include_router(router, prefix="/api/v1")

    @app.get("/api/v1/probe")
    def probe(user=Depends(dependencies._resolve_current_user)):
        if user is None:
            raise HTTPException(401, "Not authenticated")
        state.route_calls += 1
        return {"id": user["id"]}

    @app.get("/api/v1/explode")
    def explode():
        state.route_calls += 1
        raise RuntimeError("downstream route failed")

    _register_http_middleware(app)
    token = auth.create_access_token({"sub": "alerts-test", "role": "admin"})
    with TestClient(app, raise_server_exceptions=False) as client:
        client.cookies.set("session", token)
        yield SimpleNamespace(client=client, state=state, token=token, engine=engine)


def credentials(backend, carrier):
    backend.client.cookies.clear()
    if carrier == "cookie":
        backend.client.cookies.set("session", backend.token)
        return {}
    if carrier == "bearer":
        return {"Authorization": f"Bearer {backend.token}"}
    return {"X-API-Key": "test-perimeter"}


@pytest.mark.parametrize("carrier", ["cookie", "bearer", "api-key"])
def test_downstream_exception_stays_500(backend, carrier):
    response = backend.client.get("/api/v1/explode", headers=credentials(backend, carrier))
    assert response.status_code == 500
    assert backend.state.route_calls == 1


@pytest.mark.parametrize("carrier", ["cookie", "bearer"])
def test_valid_jwt_succeeds(backend, carrier):
    response = backend.client.get("/api/v1/probe", headers=credentials(backend, carrier))
    assert response.status_code == 200
    assert response.json() == {"id": 1}


def test_invalid_cookie_is_401(backend):
    backend.client.cookies.set("session", "invalid-cookie")
    assert backend.client.get("/api/v1/probe").status_code == 401
    assert backend.state.route_calls == 0


@pytest.mark.parametrize("carrier", ["cookie", "bearer"])
def test_last_seen_failure_rolls_back_and_authenticates(backend, monkeypatch, caplog, carrier):
    from core import dependencies

    def failed_write(db, jti):
        db.execute(text("UPDATE users SET is_active=0 WHERE id=1"))
        raise RuntimeError("simulated failed session write")

    monkeypatch.setattr(dependencies, "_touch_session_last_seen", failed_write)
    response = backend.client.get("/api/v1/probe", headers=credentials(backend, carrier))
    assert response.status_code == 200
    assert response.json() == {"id": 1}
    assert backend.state.rollbacks == 1
    assert "Session last_seen update failed" in caplog.text
    assert backend.token not in caplog.text
    from core import auth
    import jwt
    assert jwt.decode(backend.token, auth.SECRET_KEY, algorithms=[auth.ALGORITHM])["jti"] not in caplog.text
    with backend.engine.connect() as conn:
        assert conn.execute(text("SELECT is_active FROM users WHERE id=1")).scalar_one() == 1


@pytest.mark.parametrize("carrier", ["cookie", "bearer"])
@pytest.mark.parametrize("lookup", ["FROM token_blacklist", "FROM users WHERE username"])
def test_mandatory_auth_lookup_failure_is_503(backend, carrier, lookup):
    backend.state.fail_lookup = lookup
    headers = credentials(backend, carrier)
    # Admit at the perimeter so this exercises the route's resolver, including
    # the bearer path whose old broad catch could grant after a lookup failure.
    headers["X-API-Key"] = "test-perimeter"
    response = backend.client.get("/api/v1/probe", headers=headers)
    assert response.status_code == 503
    assert backend.state.route_calls == 0


def test_cookie_lookup_failure_cannot_fall_back_to_another_credential(backend):
    backend.state.fail_lookup = "FROM token_blacklist"
    response = backend.client.get("/api/v1/probe", headers={
        "X-API-Key": "test-perimeter", "Authorization": f"Bearer {backend.token}",
    })
    assert response.status_code == 503
    assert backend.state.route_calls == 0


@pytest.mark.parametrize("carrier", ["cookie", "bearer"])
@pytest.mark.parametrize("reason", ["revoked", "inactive"])
def test_verified_denials_remain_401(backend, carrier, reason):
    from core import auth
    import jwt

    with backend.engine.begin() as conn:
        if reason == "inactive":
            conn.execute(text("UPDATE users SET is_active=0 WHERE id=1"))
        else:
            jti = jwt.decode(backend.token, auth.SECRET_KEY, algorithms=[auth.ALGORITHM])["jti"]
            conn.execute(text("INSERT INTO token_blacklist(jti,expires_at) VALUES(:jti,'2099-01-01')"), {"jti": jti})
    assert backend.client.get("/api/v1/probe", headers=credentials(backend, carrier)).status_code == 401
    assert backend.state.route_calls == 0


def insert_alerts(engine, rows):
    defaults = dict(user_id=1, alert_type="printer_error", severity="warning", title="Alert",
                    message="Cause", printer_id=None, is_dismissed=0, created_at="2026-11-08 10:00:00")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO alerts(id,user_id,alert_type,severity,title,message,printer_id,is_dismissed,created_at) "
                          "VALUES(:id,:user_id,:alert_type,:severity,:title,:message,:printer_id,:is_dismissed,:created_at)"),
                     [dict(defaults, **row) for row in rows])


def test_alerts_normalize_legacy_rows_and_skip_unknown_classification(backend, caplog):
    insert_alerts(backend.engine, [
        dict(id=1, alert_type="SPOOL_LOW", severity="WARNING"),
        dict(id=2, alert_type="audit_integrity", severity="error"),
        dict(id=3, alert_type="print_complete", severity="success"),
        dict(id=4, alert_type="unknown-private-type", message="private message"),
        dict(id=5, severity="unknown-private-severity"),
    ])
    response = backend.client.get("/api/v1/alerts")
    assert response.status_code == 200, response.text
    rows = {row["id"]: (row["alert_type"], row["severity"]) for row in response.json()}
    assert rows == {1: ("spool_low", "warning"), 2: ("audit_integrity", "critical"), 3: ("print_complete", "info")}
    assert "Skipping unrepresentable alert id=4" in caplog.text
    assert "Skipping unrepresentable alert id=5" in caplog.text
    assert "private" not in caplog.text
    # Normalization is read-only.
    with backend.engine.connect() as conn:
        assert conn.execute(text("SELECT alert_type,severity FROM alerts WHERE id=1")).one() == ("SPOOL_LOW", "WARNING")


def test_response_accepts_every_domain_classification():
    from core.base import AlertSeverity, AlertType
    from modules.notifications.schemas import AlertResponse

    for alert_type in AlertType:
        for severity in AlertSeverity:
            row = AlertResponse(id=1, user_id=1, alert_type=alert_type, severity=severity,
                                title="Alert", created_at=datetime(2026, 11, 8))
            assert row.alert_type.value == alert_type.value
            assert row.severity.value == severity.value


def test_floor_query_keeps_all_printers_without_paging_history(backend):
    from modules.printers.models import Printer

    with Session(backend.engine) as db:
        db.add_all([Printer(id=i, name=f"Printer {i}", is_active=i != 103) for i in range(1, 104)])
        db.commit()
    # 2,500 undismissed errors for one noisy printer must not hide 101 others.
    rows = [dict(id=i, printer_id=1) for i in range(1, 2501)]
    rows += [dict(id=2500+i, printer_id=i, alert_type="PRINT_PAUSED", created_at="2026-11-01 10:00:00")
             for i in range(2, 103)]
    rows += [dict(id=3000, printer_id=2, is_dismissed=1),
             dict(id=3001, printer_id=2, user_id=2),
             dict(id=3002, printer_id=2, alert_type="print_complete"),
             dict(id=3003, printer_id=103), dict(id=3004)]
    insert_alerts(backend.engine, rows)
    response = backend.client.get("/api/v1/alerts?needs_you=true")
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result) == 102
    assert {row["printer_id"] for row in result} == set(range(1, 103))
    assert next(row["id"] for row in result if row["printer_id"] == 1) == 2500
    assert next(row["id"] for row in result if row["printer_id"] == 2) == 2502
    assert all(row["user_id"] == 1 and not row["is_dismissed"] for row in result)
    # Ordinary alert history still respects pagination.
    assert len(backend.client.get("/api/v1/alerts?limit=7&offset=10").json()) == 7
