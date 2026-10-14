"""Actual PostgreSQL live-clock and transaction-ownership evidence."""
import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from modules.printers.telemetry.bambu.ams_database_clock import sample_database_clock

engine=create_engine(os.environ['AMS_TEST_DB_URL'])
with Session(engine) as db:
    db.execute(text('CREATE TABLE clock_probe(value INTEGER)'))
    db.commit()
    db.execute(text('INSERT INTO clock_probe VALUES(1)'))
    transaction_now=db.execute(text('SELECT CURRENT_TIMESTAMP')).scalar_one()
    db.execute(text('SELECT pg_sleep(0.05)'))
    first=sample_database_clock(db)
    db.execute(text('SELECT pg_sleep(0.05)'))
    second=sample_database_clock(db)
    assert first.database_now > transaction_now
    assert second.database_now > first.database_now
    assert first.query_finished_monotonic <= second.query_started_monotonic
    assert db.in_transaction()
    db.rollback()
    assert db.execute(text('SELECT count(*) FROM clock_probe')).scalar_one()==0
engine.dispose()
print('native-ams-clock-pass')
