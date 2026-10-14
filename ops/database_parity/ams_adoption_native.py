"""Native adoption assertions, executed inside the owned offline test runtime."""
import importlib
import os
import json
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from core.ams_evidence_schema import EVIDENCE_TABLES, generations, packets, trays
from core.schema.migrator import MigrationError

migration=importlib.import_module('core.schema.migrations.020_ams_evidence')


def must_refuse(connection, mutation, expected):
    savepoint=connection.begin_nested()
    try:
        connection.execute(text(mutation))
        try:
            migration.validate(connection)
        except MigrationError as error:
            assert expected in str(error), str(error)
        else:
            raise AssertionError('adoption incorrectly accepted mutation')
    finally:
        savepoint.rollback()


def main():
    engine=create_engine(os.environ['AMS_TEST_DB_URL'])
    if os.environ.get('AMS_RESTORE_ONLY')=='1':
        with engine.begin() as connection:
            try:
                migration.validate(connection)
            except MigrationError:
                print(json.dumps({t.name:{'actual':inspect(connection).get_check_constraints(t.name),
                    'expected':migration._checks(connection,t)} for t in EVIDENCE_TABLES}))
                raise
            assert connection.execute(text('SELECT count(*) FROM ams_report_trays')).scalar_one()==1
        print('native-ams-adoption-restored-pass')
        with engine.begin() as connection:
            checks={t.name:{c['name']:migration._normalize(c['sqltext'])
                for c in inspect(connection).get_check_constraints(t.name)} for t in EVIDENCE_TABLES}
        print('restored-checks-sha256='+hashlib.sha256(json.dumps(checks,sort_keys=True).encode()).hexdigest())
        return
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE printers(id INTEGER PRIMARY KEY,name TEXT)'))
        connection.execute(text("INSERT INTO printers VALUES(1,'preserve')"))
        migration.apply(connection)
        migration.apply(connection)
        instant=datetime(2026,10,12,tzinfo=timezone.utc)
        connection.execute(generations.insert().values(generation_id='a'*32,
            printer_id=1,owner_org_id=1,configuration_key='b'*64,
            runtime_incarnation='c'*32,started_at=instant,state='ready',completed_at=instant))
        seq=connection.execute(packets.insert().values(packet_id='d'*32,generation_id='a'*32,
            received_at=instant,receipt_provenance='db-clock-estimate-v1')).inserted_primary_key[0]
        connection.execute(trays.insert().values(packet_sequence=seq,slot_number=1,
            unit_id=0,tray_id=0,material='PETG',material_state='reported'))
        migration.validate(connection)
        material_check=next(c['sqltext'] for c in inspect(connection).get_check_constraints('ams_report_trays')
                            if c['name']=='ck_ams_tray_material')
        prefix='ALTER TABLE ams_report_trays DROP CONSTRAINT ck_ams_tray_material; ALTER TABLE ams_report_trays ADD CONSTRAINT ck_ams_tray_material CHECK ('
        must_refuse(connection,prefix+material_check+') NOT VALID','native constraints')
        must_refuse(connection,prefix+material_check.replace("'PETG'","'INJECTED'")+') NOT VALID','checks differ')
        must_refuse(connection,prefix+material_check+' OR true)','checks differ')
        must_refuse(connection,"ALTER TABLE ams_report_packets DROP CONSTRAINT uq_ams_packet_id; ALTER TABLE ams_report_packets ADD CONSTRAINT uq_ams_packet_id UNIQUE NULLS NOT DISTINCT(packet_id)",'key enforcement')
        must_refuse(connection,"ALTER TABLE ams_report_packets ADD COLUMN unwanted TEXT",'columns')
        must_refuse(connection,"ALTER SEQUENCE ams_report_packets_packet_sequence_seq CYCLE",'sequence differs')
        # Sequence setval changes are not transactional. Explicitly restore the
        # owned fixture afterward, never touch an application sequence.
        try:
            must_refuse(connection,"SELECT setval('ams_report_packets_packet_sequence_seq',1,false)",
                        'sequence position')
        finally:
            connection.execute(text("SELECT setval('ams_report_packets_packet_sequence_seq',:seq,true)"),{'seq':seq})
        must_refuse(connection,"ALTER TABLE ams_report_packets DROP CONSTRAINT uq_ams_packet_id; ALTER TABLE ams_report_packets ADD CONSTRAINT uq_ams_packet_id UNIQUE(packet_id) DEFERRABLE",'key enforcement')
        must_refuse(connection,"DELETE FROM ams_report_trays",'relationship')
        must_refuse(connection,"SET LOCAL session_replication_role=replica; UPDATE ams_report_packets SET generation_id=repeat('e',32)", 'relationship')
        must_refuse(connection,"ALTER TABLE ams_current_reads ALTER CONSTRAINT fk_ams_current_generation DEFERRABLE",'foreign authority')
    # Locks persist in the caller transaction and block a competing writer.
    with engine.connect() as first, engine.connect() as second:
        transaction=first.begin()
        migration.validate(first)
        try:
            second.execute(text("SET LOCAL lock_timeout='100ms'"))
            try:
                second.execute(text("UPDATE printers SET name='must-not-land' WHERE id=1"))
            except Exception as error:
                assert getattr(error.orig,'sqlstate',None)=='55P03'
            else:
                raise AssertionError('competing writer bypassed validation lock')
        finally:
            second.rollback()
            transaction.rollback()
    with engine.begin() as connection:
        migration.validate(connection)
        assert connection.execute(text('SELECT name FROM printers')).scalar_one()=='preserve'
    # Migration itself must refuse contention. No harness lock_timeout setting
    # is applied to the validating connection.
    with engine.connect() as writer, engine.connect() as validator:
        writer.execute(text("UPDATE printers SET name='held' WHERE id=1"))
        started=time.monotonic()
        try:
            migration.validate(validator)
        except MigrationError as error:
            assert str(error)=='AMS validation authority busy'
            assert time.monotonic()-started<5
        else:
            raise AssertionError('Migration did not refuse concurrent authority')
        finally:
            validator.rollback()
            writer.rollback()
    with engine.begin() as connection:
        connection.execute(text('CREATE SCHEMA partial'))
        connection.execute(text('SET LOCAL search_path=partial'))
        connection.execute(text('CREATE TABLE printers(id INTEGER PRIMARY KEY)'))
        generations.create(connection)
        connection.execute(generations.insert().values(generation_id='e'*32,
            printer_id=1,configuration_key='b'*64,runtime_incarnation='c'*32,
            started_at=instant,state='ready',completed_at=instant))
    try:
        with engine.begin() as connection:
            connection.execute(text('SET LOCAL search_path=partial'))
            migration.apply(connection)
    except MigrationError as error:
        assert 'relationship' in str(error)
    else:
        raise AssertionError('Incomplete partial adoption accepted')
    with engine.begin() as connection:
        assert set(inspect(connection).get_table_names(schema='partial'))=={'printers','ams_read_generations'}
        connection.execute(text('DROP SCHEMA partial CASCADE'))
    from core.schema.bootstrap import bootstrap_database
    startup=create_engine(engine.url.set(database='bootstrap_probe'))
    try:
        first=bootstrap_database(startup,Path('/source/backend'))
        second=bootstrap_database(startup,Path('/source/backend'))
        assert 'python:020-ams-evidence' in first['applied']
        assert 'python:020-ams-evidence' not in second['applied']
        assert first['schema_fingerprint']==second['schema_fingerprint']
        with startup.begin() as connection:
            assert connection.execute(text("SELECT count(*) FROM odin_schema_migrations WHERE migration_id='python:020-ams-evidence'")).scalar_one()==1
    finally:
        startup.dispose()
    print('native-ams-bootstrap-rerun-pass')
    print('native-ams-adoption-pass')


if __name__=='__main__':
    main()
