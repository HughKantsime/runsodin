"""Actual caller leases; this scope grants no request or filesystem authority."""
import sqlite3
import pytest
from sqlalchemy import create_engine,event,text
from sqlalchemy.orm import Session
from core.project_import_transaction import project_import_transaction,execute
from core.schema.migrator import MigrationError
from .test_project_import_029_storage import fresh_contract

@pytest.fixture
def engine(tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'scope.db'))
    @event.listens_for(engine,'connect')
    def fk(driver,record):driver.execute('PRAGMA foreign_keys=ON')
    with engine.connect() as c:c.exec_driver_sql('BEGIN IMMEDIATE');fresh_contract(c);c.commit()
    yield engine
    engine.dispose()

@pytest.mark.parametrize('kind',['connection','session','raw'])
def test_actual_executor_scope_retains_caller_transaction(engine,kind):
    if kind=='raw':
        db=sqlite3.connect(engine.url.database);db.execute('PRAGMA foreign_keys=ON');db.execute('BEGIN IMMEDIATE')
    elif kind=='session':db=Session(engine);db.execute(text('BEGIN IMMEDIATE'))
    else:db=engine.connect();db.exec_driver_sql('BEGIN IMMEDIATE')
    try:
        with project_import_transaction(db) as scope:
            scope.fence(db);scope.check(db)
            execute(db,'INSERT INTO projects(id,name) VALUES(?,?)',(9,'owned'))
            assert scope.fenced is True
        assert db.in_transaction() if kind!='raw' else db.in_transaction
        db.rollback()
        with engine.connect() as observer:assert observer.exec_driver_sql('SELECT count(*) FROM projects').scalar_one()==0
    finally:db.close()

@pytest.mark.parametrize('kind',['connection','session','raw'])
def test_actual_commit_then_rebegin_cannot_revive_scope(engine,kind):
    if kind=='raw':db=sqlite3.connect(engine.url.database);db.execute('PRAGMA foreign_keys=ON');db.execute('BEGIN IMMEDIATE')
    elif kind=='session':db=Session(engine);db.execute(text('BEGIN IMMEDIATE'))
    else:db=engine.connect();db.exec_driver_sql('BEGIN IMMEDIATE')
    try:
        with pytest.raises(MigrationError):
            with project_import_transaction(db) as scope:
                scope.fence(db);db.commit()
                if kind=='session':db.execute(text('BEGIN IMMEDIATE'))
                elif kind=='connection':db.exec_driver_sql('BEGIN IMMEDIATE')
                else:db.execute('BEGIN IMMEDIATE')
                scope.check(db)
    finally:db.rollback();db.close()


def test_sqlalchemy_logical_only_is_not_physical_writer(engine):
    with Session(engine) as db:
        db.begin()
        with pytest.raises(MigrationError):
            with project_import_transaction(db):pass
        assert not db.connection().connection.driver_connection.in_transaction


def test_missing_fk_and_temp_shadow_refuse(engine):
    with engine.connect() as db:
        db.exec_driver_sql('PRAGMA foreign_keys=OFF');db.exec_driver_sql('BEGIN IMMEDIATE')
        with pytest.raises(MigrationError):
            with project_import_transaction(db) as scope:scope.fence(db)
        db.rollback();db.exec_driver_sql('PRAGMA foreign_keys=ON');db.exec_driver_sql('BEGIN IMMEDIATE')
        db.exec_driver_sql('CREATE TEMP TABLE print_files(id INTEGER)')
        with pytest.raises(MigrationError):
            with project_import_transaction(db) as scope:scope.fence(db)
        db.rollback()


def test_nested_replacement_and_closed_scope_refuse(engine):
    with engine.connect() as db:
        db.exec_driver_sql('BEGIN IMMEDIATE')
        with db.begin_nested():
            with pytest.raises(MigrationError):
                with project_import_transaction(db):pass
        with project_import_transaction(db) as scope:
            scope.fence(db)
            with engine.connect() as replacement:
                replacement.exec_driver_sql('BEGIN')
                with pytest.raises(MigrationError):scope.check(replacement)
        with pytest.raises(MigrationError):scope.check(db)
        db.rollback()


def test_lookalike_executor_not_accepted(engine):
    class Fake:
        _postgres=False
        in_transaction=True
    with pytest.raises(MigrationError):
        with project_import_transaction(Fake()):pass
