"""The Education-mode switch reads back the same on both database dialects."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_cert_E_notif_postgres import pg_engine  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401

URL = '/api/settings/education-mode'


@pytest.fixture(params=['sqlite', pytest.param('postgresql', marks=pytest.mark.postgresql)])
def mode_db(request):
    if request.param == 'sqlite':
        yield request.getfixturevalue('review_db')
    else:
        engine = request.getfixturevalue('pg_engine')
        with engine.connect() as connection, connection.begin():
            with Session(connection, join_transaction_mode='create_savepoint') as db:
                yield db
            # The route commits its session; discard this test's rows afterwards.
            connection.rollback()


@pytest.fixture
def mode_client(mode_db):
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.system import routes_settings

    mode_db.execute(text("DELETE FROM system_config WHERE key='education_mode'"))
    mode_db.commit()
    app = FastAPI()
    app.include_router(routes_settings.router, prefix='/api')

    def session():
        yield mode_db

    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = lambda: {
        'id': 3, 'username': 'admin', 'role': 'admin', 'group_id': None,
    }
    with TestClient(app) as client:
        yield client


def test_switch_reads_back_what_was_set(mode_client, mode_db):
    from core.education_mode import education_mode_enabled
    from core.recipient_policy import education_notification_scope

    assert mode_client.get(URL).json() == {'enabled': False}
    assert education_mode_enabled(mode_db) is False
    for wanted in (True, False, True):
        assert mode_client.put(URL, json={'enabled': wanted}).json() == {'enabled': wanted}
        # Released code compared the stored value with the text "true"; PostgreSQL returns a boolean.
        assert mode_client.get(URL).json() == {'enabled': wanted}
        assert education_mode_enabled(mode_db) is wanted
    assert education_notification_scope(mode_db) is True


@pytest.mark.parametrize('stored,expected', [
    ('true', True), ('"true"', True), (True, True), ('TRUE', True),
    ('false', False), ('"false"', False), (False, False), (None, False), ('', False), (1, False), ('yes', False),
])
def test_stored_value_shapes(stored, expected):
    from core.education_mode import stored_true
    assert stored_true(stored) is expected


def test_readiness_uses_the_same_reading():
    """The readiness projection must not carry its own comparison again."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / 'backend'
    for relative in ('modules/organizations/education_readiness.py', 'modules/system/routes_settings.py',
                     'core/recipient_policy.py'):
        source = (root / relative).read_text()
        assert 'education_mode_enabled' in source, relative
        assert '== "true"' not in source.split('education_mode')[1][:400], relative
