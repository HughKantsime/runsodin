"""Actual SQLite pretransfer byte admission; no ownership acceptance claim."""
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from modules.archives.valuation_payload import load_bounded_snapshot


@pytest.fixture
def db():
    engine = create_engine('sqlite://')
    with Session(engine) as session:
        session.execute(text('CREATE TABLE print_jobs(id INTEGER PRIMARY KEY,economic_birth_nonce TEXT,economic_uid TEXT,spool_snapshot TEXT)'))
        session.execute(text("INSERT INTO print_jobs VALUES(1,'birth','uid',NULL)"))
        yield session
    engine.dispose()


def load(db):
    return load_bounded_snapshot(db,attempt_id=1,birth_nonce='birth',identity_uid='uid')


@pytest.mark.parametrize('payload',[None,'x'*65536,'é'*32768])
def test_null_and_exact_utf8_boundary(db,payload):
    db.execute(text('UPDATE print_jobs SET spool_snapshot=:value'),{'value':payload})
    assert load(db)==payload


@pytest.mark.parametrize('payload',['x'*65537,'é'*32769])
def test_oversized_database_value_never_returned(db,payload):
    db.execute(text('UPDATE print_jobs SET spool_snapshot=:value'),{'value':payload})
    with pytest.raises(ValueError,match='unavailable or changed'):load(db)


@pytest.mark.parametrize('field',['economic_birth_nonce','economic_uid'])
def test_changed_lifetime_is_not_numeric_id_fallback(db,field):
    db.execute(text(f'UPDATE print_jobs SET {field}=:value'),{'value':'changed'})
    with pytest.raises(ValueError,match='unavailable or changed'):load(db)


def test_deleted_attempt_is_not_missing_snapshot(db):
    db.execute(text('DELETE FROM print_jobs'))
    with pytest.raises(ValueError,match='unavailable or changed'):load(db)
