"""String filters must reject unknown native enum values on both databases.

PostgreSQL uses the shared owned-container fixture. Run SQLite alone with
-m 'not postgresql'.
"""
import csv
import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from core.base import AlertSeverity, AlertType, JobStatus, OrderStatus, SpoolStatus
from core.db import get_db
from core.dependencies import get_current_user
from core.schema import bootstrap_database
from modules.inventory.models import FilamentLibrary, Spool
from modules.inventory.routes import spools
from modules.jobs.models import Job
from modules.notifications.models import Alert
from modules.notifications.routes import alerts
from modules.orders.models import Order
from modules.orders.routes import orders_crud
from modules.reporting.routes import exports
from tests.test_contracts.test_cert_E_notif_postgres import pg_engine  # noqa: F401


FILTERS = [
    ("/export/jobs", "status", JobStatus),
    ("/spools", "status", SpoolStatus),
    ("/orders", "status_filter", OrderStatus),
    ("/alerts", "alert_type", AlertType),
    ("/alerts", "severity", AlertSeverity),
]


@pytest.fixture(scope="module", params=["sqlite", pytest.param("postgresql", marks=pytest.mark.postgresql)])
def engine(request, tmp_path_factory):
    if request.param == "postgresql":
        yield request.getfixturevalue("pg_engine")
        return
    engine = create_engine(f"sqlite:///{tmp_path_factory.mktemp('enum-filters') / 'test.db'}")
    bootstrap_database(engine)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) "
                                "VALUES(1,'enum-test','unused','admin',TRUE)"))
    yield engine
    engine.dispose()


@pytest.fixture
def db(engine):
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            # sqlite3 legacy transaction mode does not emit BEGIN before SAVEPOINT.
            if engine.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN")
            with Session(bind=connection, join_transaction_mode="create_savepoint") as db:
                _seed_rows(db)
                yield db
        finally:
            transaction.rollback()


def _seed_rows(db):
    filament = FilamentLibrary(brand="Test", name="PLA")
    db.add(filament)
    db.flush()
    db.add_all([Job(id=i, item_name=f"Job {i}", status=value)
                for i, value in enumerate(JobStatus, 1)])
    db.add_all([Spool(id=i, filament_id=filament.id, status=value)
                for i, value in enumerate(SpoolStatus, 1)])
    db.add_all([Order(id=i, order_number=f"Order {i}", status=value)
                for i, value in enumerate(OrderStatus, 1)])
    # Every type and severity is represented, with stable IDs on both engines.
    db.add_all([Alert(id=i, user_id=1, alert_type=value,
                      severity=list(AlertSeverity)[(i - 1) % len(AlertSeverity)], title=f"Alert {i}")
                for i, value in enumerate(AlertType, 1)])
    db.commit()


@pytest.fixture
def client(db, monkeypatch):
    app = FastAPI()
    for module in (exports, spools, orders_crud, alerts):
        app.include_router(module.router)

    def session():
        yield db

    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = lambda: {"id": 1, "role": "admin", "group_id": None}
    monkeypatch.setattr(exports, "require_feature", lambda *_: None)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def rows(response, path):
    assert response.status_code == 200, response.text
    if path == "/export/jobs":
        return [{"id": int(row["ID"]), "status": row["Status"]}
                for row in csv.DictReader(io.StringIO(response.text))]
    return response.json()


@pytest.mark.parametrize("path,parameter,enum", FILTERS)
@pytest.mark.parametrize("value", ["not-a-member", "PENDING", " pending "])
def test_unknown_filter_returns_plain_422(client, path, parameter, enum, value):
    response = client.get(path, params={parameter: value})
    assert response.status_code == 422, response.text
    assert response.json() == {"detail": f"{parameter} must be one of: {', '.join(e.value for e in enum)}."}


@pytest.mark.parametrize("path,parameter,enum,value", [
    (path, parameter, enum, value.value) for path, parameter, enum in FILTERS for value in enum
])
def test_valid_filter_returns_matching_rows(client, path, parameter, enum, value):
    result = rows(client.get(path, params={parameter: value}), path)
    field = "status" if parameter == "status_filter" else parameter
    expected = ([i for i, _ in enumerate(AlertType, 1)
                 if list(AlertSeverity)[(i - 1) % len(AlertSeverity)].value == value]
                if parameter == "severity" else [list(enum).index(enum(value)) + 1])
    assert sorted(row["id"] for row in result) == expected
    assert all(row[field] == value for row in result)


@pytest.mark.parametrize("path,parameter,enum", FILTERS)
def test_absent_filter_returns_all_rows(client, path, parameter, enum):
    result = rows(client.get(path), path)
    count = len(AlertType) if parameter == "severity" else len(enum)
    assert sorted(row["id"] for row in result) == list(range(1, count + 1))


@pytest.mark.parametrize("path,parameter,enum", FILTERS)
def test_empty_filter_returns_all_rows(client, path, parameter, enum):
    result = rows(client.get(path, params={parameter: ""}), path)
    count = len(AlertType) if parameter == "severity" else len(enum)
    assert sorted(row["id"] for row in result) == list(range(1, count + 1))


def test_fixture_rows_are_isolated(client, engine):
    with Session(engine) as outside:
        for model in (Job, Spool, Order, Alert, FilamentLibrary):
            assert outside.query(model).count() == 0


@pytest.mark.parametrize("path,parameter,enum", FILTERS)
def test_openapi_filter_remains_optional_string(client, path, parameter, enum):
    parameters = client.get("/openapi.json").json()["paths"][path]["get"]["parameters"]
    param = next(p for p in parameters if p["name"] == parameter)
    assert param["required"] is False
    assert param["schema"]["anyOf"] == [{"type": "string"}, {"type": "null"}]
