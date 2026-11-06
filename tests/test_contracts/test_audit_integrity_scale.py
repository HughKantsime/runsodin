"""Exercise the reported heavy-user and SQLite retention failure sizes."""
import time
from sqlalchemy import text
from sqlalchemy.orm import Session
from core.audit_erasure import rewrite
from core.audit_retention import retain
from core.audit_integrity import verify
from tests.test_contracts.test_audit_integrity_e1 import engine


def test_70001_row_erasure_then_300001_row_retention(engine, record_property):
    with engine.begin() as c:
        # Bounded fixture generation avoids hiding production allocation costs.
        insert = text("INSERT INTO audit_logs(timestamp,action,ip_address) VALUES('2000-01-01 00:00:00','scale','192.0.2.1')")
        for _ in range(300):
            c.execute(insert, [{}]*1000)
        c.execute(insert)
    started = time.monotonic()
    with Session(engine) as db:
        rewrite(db, 'UPDATE audit_logs SET ip_address=NULL WHERE id<=:id', {'id': 70001})
        db.commit()
        assert db.execute(text('SELECT count(*) FROM audit_redactions')).scalar_one() == 70001
        assert db.execute(text('SELECT count(*) FROM audit_logs WHERE id<=70001 AND ip_address IS NOT NULL')).scalar_one() == 0
    record_property('erasure_70001_seconds', time.monotonic()-started)
    started = time.monotonic()
    with Session(engine) as db:
        assert retain(db, 'timestamp<:cutoff', '2020-01-01') == 300001
        db.commit()
        assert verify(db.connection())['ok']
        assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='scale'")).scalar_one() == 0
    record_property('retention_300001_seconds', time.monotonic()-started)
