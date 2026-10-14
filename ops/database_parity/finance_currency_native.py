"""Real PostgreSQL currency service, scoped upsert contention and audit atomicity."""
import importlib
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import sleep
from uuid import uuid4
from sqlalchemy import create_engine, text, event
from sqlalchemy.orm import Session
from core.finance_currency import read_currency, write_currency, SUPPORTED_CURRENCIES
from core.models import AuditLog


def main():
    engine=create_engine(os.environ['ODIN_OWNED_CURRENCY_DB'],hide_parameters=True)
    schema='currency_'+uuid4().hex
    with engine.begin() as c:c.exec_driver_sql('CREATE SCHEMA '+schema)
    scoped=create_engine(os.environ['ODIN_OWNED_CURRENCY_DB'],connect_args={'options':'-csearch_path='+schema},hide_parameters=True)
    try:
        with scoped.begin() as c:
            importlib.import_module('core.schema.migrations.009_cost_foundation').apply(c)
            AuditLog.__table__.create(c)
        with Session(scoped) as s:
            assert read_currency(s,owner_scope='org',owner_key=7) is None
            assert s.execute(text('SELECT count(*) FROM finance_currency_settings')).scalar_one()==0
            write_currency(s,owner_scope='org',owner_key=7,currency='JPY');s.rollback()
        with Session(scoped) as s:
            assert read_currency(s,owner_scope='org',owner_key=7) is None
            assert s.execute(text('SELECT count(*) FROM audit_logs')).scalar_one()==0
        def reject(conn,cursor,statement,parameters,context,many):
            if statement.lstrip().upper().startswith('INSERT INTO AUDIT_LOGS'):raise RuntimeError('Synthetic audit flush failure')
        event.listen(scoped,'before_cursor_execute',reject)
        try:
            with Session(scoped) as s:
                write_currency(s,owner_scope='global',owner_key=0,currency='CHF')
                try:s.commit()
                except RuntimeError:s.rollback()
                else:raise AssertionError('Audit flush failure expected')
        finally:event.remove(scoped,'before_cursor_execute',reject)
        with Session(scoped) as s:
            assert s.execute(text('SELECT count(*) FROM finance_currency_settings')).scalar_one()==0
        entered,attempted,release=Event(),Event(),Event()
        contender_pid=[]
        def held():
            with Session(scoped) as s:
                write_currency(s,owner_scope='org',owner_key=7,currency='USD');entered.set()
                assert release.wait(5);s.commit()
        def competing():
            assert entered.wait(5)
            with Session(scoped) as s:
                contender_pid.append(s.execute(text('SELECT pg_backend_pid()')).scalar_one())
                attempted.set();write_currency(s,owner_scope='org',owner_key=7,currency='CAD');s.commit()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(held);second=pool.submit(competing)
            try:
                assert attempted.wait(5)
                for _ in range(30):
                    with engine.connect() as c:
                        waiting=c.execute(text('SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid'),{'pid':contender_pid[0]}).scalar_one()
                    if waiting=='Lock':break
                    sleep(.05)
                assert waiting=='Lock' and not second.done()
            finally:release.set()
            first.result();second.result()
        with Session(scoped) as s:
            assert read_currency(s,owner_scope='org',owner_key=7)=='CAD'
            assert s.execute(text('SELECT count(*) FROM finance_currency_settings')).scalar_one()==1
            assert s.execute(text('SELECT count(*) FROM audit_logs')).scalar_one()==2
            for currency in (*SUPPORTED_CURRENCIES,None):
                write_currency(s,owner_scope='global',owner_key=0,currency=currency);s.commit()
                assert read_currency(s,owner_scope='global',owner_key=0)==currency
            assert read_currency(s,owner_scope='org',owner_key=7)=='CAD'
            write_currency(s,owner_scope='org',owner_key=7,currency='CAD');s.commit()
            assert s.execute(text('SELECT count(*) FROM finance_currency_settings')).scalar_one()==2
        print('finance-currency-native-pass: read-only absence, all codes/null, tenant isolation, caller/audit rollback, observed real Lock wait, one-row concurrent upsert and replay')
    finally:
        scoped.dispose()
        with engine.begin() as c:c.exec_driver_sql('DROP SCHEMA '+schema+' CASCADE')
        engine.dispose()

if __name__=='__main__':main()
