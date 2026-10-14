"""Native SQLite constraints; PostgreSQL DDL checks are not native acceptance."""
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateTable

from core.ams_evidence_schema import EVIDENCE_TABLES, generations, current_reads, packets, trays, SCHEMA_MATERIALS


@pytest.fixture
def connection():
    engine = create_engine('sqlite:///:memory:')
    @event.listens_for(engine, 'connect')
    def foreign_keys(dbapi, _):
        dbapi.execute('PRAGMA foreign_keys=ON')
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE printers(id INTEGER PRIMARY KEY)'))
        conn.execute(text('INSERT INTO printers VALUES(1),(2)'))
        for table in EVIDENCE_TABLES:
            table.create(conn)
        yield conn
    engine.dispose()


def generation(connection, **changes):
    fields = dict(generation_id='a'*32, printer_id=1, owner_org_id=1,
                  configuration_key='b'*64, runtime_incarnation='c'*32,
                  started_at=datetime(2026, 10, 12, tzinfo=timezone.utc),
                  state='acquiring')
    fields.update(changes)
    connection.execute(generations.insert().values(**fields))


def packet(connection, key='d'*32):
    return connection.execute(packets.insert().values(
        packet_id=key, generation_id='a'*32,
        received_at=datetime(2026, 10, 12, tzinfo=timezone.utc),
        receipt_provenance='db-clock-estimate-v1')).inserted_primary_key[0]


def test_cross_printer_and_cross_runtime_pointer_refuse(connection):
    generation(connection)
    for changes in (dict(printer_id=2), dict(runtime_incarnation='f'*32)):
        fields = dict(printer_id=1, generation_id='a'*32, runtime_incarnation='c'*32)
        fields.update(changes)
        with pytest.raises(IntegrityError):
            connection.execute(current_reads.insert().values(**fields))
    connection.execute(current_reads.insert().values(
        printer_id=1, generation_id='a'*32, runtime_incarnation='c'*32))
    connection.execute(text('DELETE FROM printers WHERE id=1'))
    assert connection.execute(text('SELECT count(*) FROM ams_current_reads')).scalar_one() == 0
    assert connection.execute(text('SELECT count(*) FROM ams_read_generations')).scalar_one() == 1


@pytest.mark.parametrize('changes', [
    dict(generation_id='Z'*32), dict(printer_id=0), dict(owner_org_id=0),
    dict(configuration_key='x'*64), dict(runtime_incarnation='short'),
    dict(state='ready'), dict(state='invalidated'), dict(state='other'),
    dict(state='failed', completed_at=datetime(2026,10,11,tzinfo=timezone.utc)),
    dict(completed_at=datetime(2026,10,12,tzinfo=timezone.utc)),
])
def test_malformed_generation_refuses(connection, changes):
    with pytest.raises(IntegrityError):
        generation(connection, **changes)


@pytest.mark.parametrize('state', ['ready', 'failed', 'invalidated'])
def test_terminal_phase_has_chronological_completion(connection, state):
    end = datetime(2026,10,12,tzinfo=timezone.utc) + timedelta(seconds=1)
    generation(connection, state=state, completed_at=end,
               invalidated_at=end if state == 'invalidated' else None)


def test_deleted_packet_sequence_is_not_reused(connection):
    generation(connection)
    old = packet(connection)
    connection.execute(packets.delete())
    assert packet(connection, 'e'*32) > old


@pytest.mark.parametrize('changes', [dict(slot_number=2), dict(unit_id=4),
    dict(tray_id=-1), dict(material='petg'), dict(material=None),
    dict(material_state='unknown'), dict(mechanically_ready=1)])
def test_bad_physical_material_and_readiness_facts_refuse(connection, changes):
    generation(connection)
    sequence = packet(connection)
    fields = dict(packet_sequence=sequence, slot_number=1, unit_id=0, tray_id=0,
                  material='PETG', material_state='reported')
    fields.update(changes)
    with pytest.raises(IntegrityError):
        connection.execute(trays.insert().values(**fields))


def test_unknown_packet_does_not_overwrite_original_report(connection):
    generation(connection)
    first = packet(connection)
    second = packet(connection, 'e'*32)
    for seq, material, state in ((first,'PETG','reported'),(second,None,'unknown')):
        connection.execute(trays.insert().values(packet_sequence=seq,
            slot_number=1, unit_id=0, tray_id=0, material=material, material_state=state))
    rows = connection.execute(text('SELECT material,material_state FROM ams_report_trays ORDER BY packet_sequence')).all()
    assert rows == [('PETG','reported'), (None,'unknown')]
    with pytest.raises(IntegrityError):
        packet(connection, 'e'*32)


def test_postgresql_uses_noncycling_identity_and_composite_pointer_fk():
    dialect = postgresql.dialect()
    packet_ddl = str(CreateTable(packets).compile(dialect=dialect))
    assert 'GENERATED ALWAYS AS IDENTITY (NO CYCLE)' in packet_ddl
    pointer_ddl = str(CreateTable(current_reads).compile(dialect=dialect))
    assert 'FOREIGN KEY(generation_id, printer_id, runtime_incarnation)' in pointer_ddl
    assert 'ON DELETE CASCADE' in pointer_ddl


def test_frozen_schema_vocabulary_matches_current_supported_materials():
    from core.material_identity import _MATERIALS
    assert set(SCHEMA_MATERIALS) == _MATERIALS
