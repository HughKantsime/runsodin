"""Actual SQLite clock query and refusal without timestamp renewal."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import time
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from modules.printers.telemetry.bambu import ams_database_clock as module

RUNTIME=SimpleNamespace(clock_domain='synthetic-domain')


def test_actual_sqlite_clock_preserves_caller_transaction(monkeypatch):
    engine=create_engine('sqlite://')
    with Session(engine) as db:
        db.execute(text('CREATE TABLE retained(value INTEGER)'))
        db.commit()
        db.execute(text('INSERT INTO retained VALUES(1)'))
        anchor=module.sample_database_clock(db)
        assert anchor.database_now.tzinfo is timezone.utc
        assert anchor.query_started_monotonic <= anchor.query_finished_monotonic
        assert db.in_transaction()
        db.rollback()
        assert db.execute(text('SELECT count(*) FROM retained')).scalar_one()==0
    engine.dispose()


class FixedDB:
    def __init__(self,dialect,value):self.dialect=dialect;self.value=value;self.queries=[]
    def get_bind(self):return SimpleNamespace(dialect=SimpleNamespace(name=self.dialect))
    def execute(self,query):
        self.queries.append(str(query))
        return SimpleNamespace(scalar_one=lambda:self.value)


def test_sqlite_rounding_is_explicitly_conservative():
    anchor=module.sample_database_clock(FixedDB('sqlite','2026-10-12T12:00:00.123Z'))
    assert anchor.database_now==datetime(2026,10,12,12,0,0,122000,tzinfo=timezone.utc)


def test_postgresql_uses_live_clock_not_transaction_timestamp():
    now=datetime(2026,10,12,tzinfo=timezone.utc)
    db=FixedDB('postgresql',now)
    assert module.sample_database_clock(db).database_now==now
    assert db.queries==['SELECT clock_timestamp()']


@pytest.mark.parametrize('dialect,value',[('mysql',None),('sqlite',None),('sqlite','invalid'),
    ('postgresql',datetime(2026,10,12)),('postgresql','invalid')])
def test_unavailable_clock_has_no_wall_time_fallback(dialect,value):
    with pytest.raises(ValueError,match='^AMS database clock unavailable$'):
        module.sample_database_clock(FixedDB(dialect,value))


@pytest.mark.parametrize('offset',[-1,0,1])
def test_generation_chronology_never_clamps_receipt(monkeypatch,offset):
    now=datetime(2026,10,12,tzinfo=timezone.utc)
    monkeypatch.setattr(module,'revalidate_runtime_identity',lambda value:None)
    monkeypatch.setattr(module,'sample_database_clock',lambda db:module.DatabaseClockAnchor(now,20,20.5))
    received=now-timedelta(seconds=10.5)
    args=dict(received_monotonic=10,receipt_clock_domain=RUNTIME.clock_domain,runtime=RUNTIME,
              generation_started_at=received+timedelta(microseconds=offset))
    if offset>0:
        with pytest.raises(ValueError,match='^AMS receipt chronology unavailable$'):
            module.anchor_original_receipt(None,**args)
    else:assert module.anchor_original_receipt(None,**args).received_at==received


def test_runtime_replacement_during_query_refuses(monkeypatch):
    checks=[]
    def validate(value):
        checks.append(value)
        if len(checks)==2:raise ValueError('synthetic failure')
    monkeypatch.setattr(module,'revalidate_runtime_identity',validate)
    monkeypatch.setattr(module,'sample_database_clock',lambda db:module.DatabaseClockAnchor(datetime.now(timezone.utc),10,10))
    with pytest.raises(ValueError,match='^AMS receipt chronology unavailable$'):
        module.anchor_original_receipt(None,received_monotonic=10,receipt_clock_domain=RUNTIME.clock_domain,
            runtime=RUNTIME,generation_started_at=datetime(2026,1,1,tzinfo=timezone.utc))
