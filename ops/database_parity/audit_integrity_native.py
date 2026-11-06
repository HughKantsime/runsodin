"""Owned PostgreSQL contention/erasure proof, called by the post027 restore runner."""
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import text
from sqlalchemy.orm import Session
from core.audit import log_audit
from core.audit_integrity import verify, canonical_projection, content_hash, row
from core.audit_snapshot import snapshot
import json
from core.audit_erasure import rewrite


def exercise(engine):
    assert engine.dialect.name == 'postgresql'
    with engine.begin() as c:
        initial = verify(c)
        assert initial['ok']

    def append(index):
        with Session(engine) as db:
            db.execute(text("SET LOCAL statement_timeout='30s'"))
            for _ in range(5):
                if index % 2:
                    log_audit(db, 'e1.native', details={'worker': index}, ip='192.0.2.42')
                else:
                    db.execute(text("INSERT INTO audit_logs(action,ip_address) VALUES('e1.native','192.0.2.42')"))
            db.commit()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(append, range(16)))
    with engine.begin() as c:
        result = verify(c)
        assert result['ok'] and result['rows'] == initial['rows']+80
        target = c.execute(text("SELECT min(id) FROM audit_logs WHERE action='e1.native'")).scalar_one()
    with engine.connect() as c:
        tx = c.begin()
        try:
            c.execute(text("UPDATE audit_logs SET action='tampered' WHERE id=:id"), {'id': target})
            assert verify(c)['first_break'] == target
        finally:
            tx.rollback()
    with Session(engine) as db:
        rewrite(db, 'UPDATE audit_logs SET ip_address=NULL WHERE id=:id', {'id': target})
        db.commit()
        assert verify(db.connection())['ok']
    with snapshot(engine) as c:
        assert c.exec_driver_sql('SHOW transaction_isolation').scalar_one() == 'repeatable read'
        assert c.exec_driver_sql('SHOW transaction_read_only').scalar_one() == 'on'
        prior = verify(c)
        append(1)  # completes on another connection while read snapshot remains open
        assert verify(c) == prior
        record = c.execute(text(f"SELECT {canonical_projection(c)} FROM audit_logs a WHERE action='e1.native' AND hash_salt IS NOT NULL LIMIT 1")).mappings().one()
        assert isinstance(record['details'], (str, type(None)))
        assert len(record['timestamp']) == 26 and content_hash(record) == record['row_hash']
    with engine.connect() as c:
        tx = c.begin()
        try:
            c.execute(text("UPDATE audit_logs SET details='{}' WHERE id=:id"), {'id': target})
            evidence = json.loads(c.execute(text('SELECT evidence FROM audit_redactions WHERE row_id=:id'), {'id': target}).scalar_one())
            evidence['after_content_hash'] = row(c, target)['content_hash']
            c.execute(text('UPDATE audit_redactions SET evidence=:d WHERE row_id=:id'), {'d': json.dumps(evidence), 'id': target})
            assert verify(c)['first_break'] == target
        finally:
            tx.rollback()
    # A corruption does not deny GDPR erasure, and the receipt cannot launder it.
    with engine.connect() as c:
        tx = c.begin()
        try:
            c.execute(text("UPDATE audit_logs SET action='tampered' WHERE id=:id"), {'id': target+1})
            with Session(bind=c) as db:
                rewrite(db, 'UPDATE audit_logs SET ip_address=NULL WHERE id=:id', {'id': target+1})
                assert not verify(c)['ok']
        finally:
            tx.rollback()
    exercise_siem_close(engine)
    print('audit-integrity-native-contention-tamper-erasure-snapshot-payload-receipt-pass', flush=True)


def exercise_siem_close(engine):
    """A nested close must reserve outbox space before the outer append does."""
    with engine.connect() as c:
        tx = c.begin()
        try:
            # Close any pre-existing fixture gap before measuring this owned stretch.
            c.execute(text('UPDATE audit_chain_state SET siem_until=4102444800'))
            c.execute(text("INSERT INTO audit_logs(action) VALUES('e1.native.gap-baseline')"))
            c.execute(text("INSERT INTO audit_siem_destinations(id,name,kind,endpoint,enabled,tls) "
                "VALUES('e1-native-gap','Owned gap proof','syslog','syslog://192.0.2.10:514',1,0)"))
            c.execute(text('UPDATE audit_chain_state SET siem_until=0'))
            for _ in range(3):
                c.execute(text("INSERT INTO audit_logs(action) VALUES('e1.native.gap')"))
            gap = c.execute(text('SELECT * FROM audit_siem_gaps WHERE closed=0')).mappings().one()
            c.execute(text('DELETE FROM audit_siem_outbox'))
            c.execute(text("INSERT INTO audit_siem_outbox(destination_id,audit_id) "
                "SELECT 'e1-cap-'||n,:id FROM generate_series(1,10000) AS n"), {'id': gap['first_id']})
            c.execute(text('UPDATE audit_chain_state SET siem_until=4102444800'))
            c.execute(text("INSERT INTO audit_logs(action) VALUES('e1.native.renewed')"))
            close = c.execute(text("SELECT id,details FROM audit_logs "
                "WHERE action='siem.gap_closed' ORDER BY chain_seq DESC LIMIT 1")).mappings().one()
            details = json.loads(close['details']) if isinstance(close['details'], str) else close['details']
            assert details['gap_id'] == gap['gap_id'] and details['skipped'] == 3
            assert c.execute(text('SELECT count(*) FROM audit_siem_outbox')).scalar_one() <= 10000
            assert c.execute(text("SELECT 1 FROM audit_siem_outbox WHERE destination_id='e1-native-gap' "
                                  'AND audit_id=:id'), {'id': close['id']}).first()
            assert verify(c)['ok']
        finally:
            tx.rollback()
