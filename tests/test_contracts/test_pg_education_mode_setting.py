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


def test_one_reader_of_the_stored_switch():
    """No file but core/education_mode.py selects the stored value, so none can carry its
    own comparison again (two did, and read PostgreSQL's boolean as off)."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / 'backend'
    select = re.compile(r"SELECT\s+value\s+FROM\s+system_config\s+WHERE\s+key\s*=\s*'education_mode'", re.I)
    readers = sorted(str(path.relative_to(root)) for path in root.rglob('*.py')
                     if not any(part.startswith('preserved') for part in path.parts)
                     and select.search(path.read_text()))
    assert readers == ['core/education_mode.py']
    for relative in ('modules/organizations/education_readiness.py', 'modules/system/routes_settings.py',
                     'core/recipient_policy.py'):
        assert 'education_mode_enabled(db)' in (root / relative).read_text(), relative
