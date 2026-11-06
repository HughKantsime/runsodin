"""Existing-connection save binds nullable values on the real PostgreSQL driver."""
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from core.schema import bootstrap_database
from modules.organizations import education_roster_store as store
from modules.organizations.routes_education_roster import Configuration
from tests.test_contracts.test_education_classes_migration import pg_engine


def check_existing_connection_saves(engine, monkeypatch):
    monkeypatch.setenv('ENCRYPTION_KEY', Fernet.generate_key().decode())
    bootstrap_database(engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO groups(id,name,is_org) VALUES(990050,'Save school',TRUE)"))
        conn.execute(text("INSERT INTO users(id,username,email,password_hash,role,is_active,group_id) "
            "VALUES(990050,'save-admin','save@example.test','','admin',TRUE,990050),"
            "(990051,'connector','connector@example.test','','admin',TRUE,990050)"))
    principal = {'id': 990050, 'group_id': 990050, 'role': 'admin'}
    with Session(engine) as db:
        saved = store.save(db, principal, Configuration(provider='google', enabled=True, config={
            'client_id': 'fixture-client', 'client_secret': 'fixture-secret',
            'public_origin': 'https://odin.example.test', 'allowed_domains': ['example.test']}))
        # No token yet: both nullable assignments must work on PostgreSQL too.
        saved = store.save(db, principal, Configuration(provider='google', enabled=True,
            revision=saved['revision'], config={}))
        assert not saved['connected']
        assert store.load(db, saved['id'])['connected_by'] is None
        token = store.pack({'refresh_token': 'fixture-refresh'})
        db.execute(text('UPDATE education_roster_connections SET tokens_encrypted=:t,connected_by=:a WHERE id=:c'),
                   {'t': token, 'a': 990051, 'c': saved['id']})
        db.commit()
        saved = store.save(db, principal, Configuration(provider='google', enabled=True,
            revision=saved['revision'], config={'allowed_domains': ['example.test', 'school.test']}))
        current = store.load(db, saved['id'])
        assert saved['connected'] and current['tokens_encrypted'] == token
        assert current['connected_by'] == 990051  # Preserve the original connector, not the editor.
        saved = store.save(db, principal, Configuration(provider='google', enabled=True,
            revision=saved['revision'], config={'client_secret': 'fixture-rotated'}))
        current = store.load(db, saved['id'])
        assert not saved['connected'] and saved['revision'] == 4
        assert current['tokens_encrypted'] is None and current['connected_by'] is None
        assert current['config']['client_secret'] == 'fixture-rotated'


def test_existing_connection_save_sqlite(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + str(tmp_path / 'save.db'))
    try:
        check_existing_connection_saves(engine, monkeypatch)
    finally:
        engine.dispose()


@pytest.mark.postgresql
@pytest.mark.integration
def test_existing_connection_save_postgresql(pg_engine, monkeypatch):
    with pg_engine.begin() as conn:
        conn.exec_driver_sql('DROP SCHEMA public CASCADE')
        conn.exec_driver_sql('CREATE SCHEMA public')
    check_existing_connection_saves(pg_engine, monkeypatch)
