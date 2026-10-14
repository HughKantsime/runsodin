"""Strict SQLite adoption and retained-record refusal; native parity separate."""
import importlib
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.schema import CreateTable

from core.ams_evidence_schema import EVIDENCE_TABLES, generations, current_reads, packets, trays
from core.schema.migrator import MigrationError

migration = importlib.import_module('core.schema.migrations.020_ams_evidence')


@pytest.fixture
def engine():
    value = create_engine('sqlite:///:memory:')
    @event.listens_for(value,'connect')
    def enable(connection,_):
        connection.execute('PRAGMA foreign_keys=ON')
    with value.begin() as conn:
        conn.execute(text('CREATE TABLE printers(id INTEGER PRIMARY KEY, name TEXT)'))
        conn.execute(text("INSERT INTO printers VALUES(1,'preserve')"))
    yield value
    value.dispose()


def test_empty_creation_and_adoption_rerun_preserve_printer(engine):
    with engine.begin() as conn:
        migration.apply(conn)
        migration.apply(conn)
        migration.validate(conn)
        assert conn.execute(text('SELECT * FROM printers')).all()==[(1,'preserve')]
        assert conn.execute(text('SELECT count(*) FROM ams_read_generations')).scalar_one()==0


@pytest.mark.parametrize('change', ['no_autoincrement','different_check','extra_column'])
def test_wrong_contract_refused_before_creating_other_tables(engine,change):
    with engine.begin() as conn:
        ddl = str(CreateTable(packets).compile(dialect=conn.dialect))
        if change=='no_autoincrement':
            ddl=ddl.replace(' AUTOINCREMENT','')
        elif change=='different_check':
            ddl=ddl.replace('packet_sequence>0','packet_sequence>=0')
        else:
            ddl=ddl.replace('packet_id VARCHAR(32)', 'extra INTEGER, packet_id VARCHAR(32)')
        conn.exec_driver_sql(ddl)
        before=set(inspect(conn).get_table_names())
        with pytest.raises(MigrationError):
            migration.apply(conn)
        assert set(inspect(conn).get_table_names())==before


def seed(conn,state='ready'):
    instant=datetime(2026,10,12,tzinfo=timezone.utc)
    conn.execute(generations.insert().values(generation_id='a'*32, printer_id=1,
        configuration_key='b'*64,runtime_incarnation='c'*32,started_at=instant,
        state=state,completed_at=instant if state!='acquiring' else None))
    return instant


def test_failed_generation_cannot_be_adopted_as_current(engine):
    with engine.begin() as conn:
        migration.apply(conn)
        seed(conn,'failed')
        conn.execute(current_reads.insert().values(printer_id=1,generation_id='a'*32,
                                                  runtime_incarnation='c'*32))
        with pytest.raises(MigrationError,match='pointer'):
            migration.validate(conn)


@pytest.mark.parametrize('state,receipt', [
    ('acquiring',datetime(2026,10,12,tzinfo=timezone.utc)),
    ('failed',datetime(2026,10,12,tzinfo=timezone.utc)),
    ('ready',datetime(2026,10,11,tzinfo=timezone.utc)),
    ('ready',datetime(2026,10,13,tzinfo=timezone.utc)),
])
def test_packet_phase_or_chronology_mismatch_refuses(engine,state,receipt):
    with engine.begin() as conn:
        migration.apply(conn)
        seed(conn,state)
        conn.execute(packets.insert().values(packet_id='d'*32,generation_id='a'*32,
            received_at=receipt,receipt_provenance='db-clock-estimate-v1'))
        conn.execute(trays.insert().values(packet_sequence=1,slot_number=1,
            unit_id=0,tray_id=0,material='PETG',material_state='reported'))
        with pytest.raises(MigrationError,match='chronology'):
            migration.validate(conn)


def test_valid_retained_evidence_is_preserved(engine):
    with engine.begin() as conn:
        migration.apply(conn)
        instant=seed(conn)
        conn.execute(packets.insert().values(packet_id='d'*32,generation_id='a'*32,
            received_at=instant,receipt_provenance='db-clock-estimate-v1'))
        conn.execute(trays.insert().values(packet_sequence=1,slot_number=1,
            unit_id=0,tray_id=0,material='PETG',material_state='reported'))
        migration.apply(conn)
        assert conn.execute(text('SELECT packet_id FROM ams_report_packets')).scalar_one()=='d'*32


def test_failed_partial_adoption_rolls_back_only_new_tables(engine):
    with engine.begin() as conn:
        generations.create(conn)
        seed(conn,'ready')  # Ready history without a packet is incomplete.
    with pytest.raises(MigrationError,match='relationship'):
        with engine.begin() as conn:
            migration.apply(conn)
    with engine.connect() as conn:
        assert set(inspect(conn).get_table_names())=={'printers','ams_read_generations'}
        assert conn.execute(text('SELECT state FROM ams_read_generations')).scalar_one()=='ready'
        assert conn.execute(text('SELECT name FROM printers')).scalar_one()=='preserve'


def test_validation_keeps_transaction_owned_by_caller(engine):
    with engine.begin() as conn:
        migration.apply(conn)
    with engine.connect() as conn:
        transaction=conn.begin()
        conn.execute(text("UPDATE printers SET name='temporary' WHERE id=1"))
        migration.validate(conn)
        transaction.rollback()
    with engine.connect() as conn:
        assert conn.execute(text('SELECT name FROM printers')).scalar_one()=='preserve'


def test_sqlite_validation_blocks_a_competing_writer(tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'owned-ams.db'),
                         connect_args={'timeout':0.05})
    try:
        with engine.begin() as conn:
            conn.execute(text('CREATE TABLE printers(id INTEGER PRIMARY KEY)'))
            conn.execute(text('INSERT INTO printers VALUES(1)'))
            migration.apply(conn)
        with engine.connect() as first, engine.connect() as second:
            migration.validate(first)
            with pytest.raises(OperationalError,match='locked'):
                second.execute(text('INSERT INTO printers VALUES(2)'))
            second.rollback()
            first.rollback()
        with engine.begin() as conn:
            conn.execute(text('INSERT INTO printers VALUES(2)'))
    finally:
        engine.dispose()


def test_real_bootstrap_records_ams_once_and_validates_on_rerun(tmp_path):
    from core.schema.bootstrap import bootstrap_database
    from pathlib import Path
    engine=create_engine('sqlite:///'+str(tmp_path/'owned-bootstrap.db'))
    try:
        root=Path(__file__).resolve().parents[2]/'backend'
        first=bootstrap_database(engine,root)
        assert 'python:020-ams-evidence' in first['applied']
        second=bootstrap_database(engine,root)
        assert 'python:020-ams-evidence' not in second['applied']
        assert first['schema_fingerprint']==second['schema_fingerprint']
        with engine.begin() as conn:
            assert conn.execute(text("SELECT count(*) FROM odin_schema_migrations WHERE migration_id='python:020-ams-evidence'")).scalar_one()==1
            conn.execute(text('ALTER TABLE ams_report_trays ADD COLUMN unwanted TEXT'))
        with pytest.raises(MigrationError,match='columns'):
            bootstrap_database(engine,root)
    finally:
        engine.dispose()
