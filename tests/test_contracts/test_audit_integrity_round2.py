"""Round-two adversaries: bounded cleanup, complete walks and external anchors."""
import base64
import json
import sqlite3
from sqlalchemy import text, event
from sqlalchemy.orm import Session
import pytest
from core.audit_integrity import verify
from core.audit_erasure import rewrite
from core.audit_retention import retain
from core.audit_receipts import seal, authentic
from modules.system.audit_integrity_job import check_daily, health
from tests.test_contracts.test_audit_integrity_e1 import engine, client, append, destination


def test_null_sequence_is_failure_and_excluded_from_known_break_export(client, engine):
    append(engine, 2)
    with engine.begin() as c:
        c.execute(text('UPDATE audit_logs SET chain_seq=NULL WHERE id=1'))
        result = verify(c)
        assert any(b['reason'] == 'unchained' and b['row_id'] == 1 for b in result['breaks'])
        assert any(b['reason'] == 'row_count' for b in result['breaks'])
    path = '/export/audit-logs/jsonl?date_from=2000-01-01&date_to=2099-01-01'
    assert client[0].get(path).status_code == 409
    check_daily(engine, force=True)
    response = client[0].get(path)
    assert response.status_code == 200, response.text
    assert all(r['chain_seq'] for r in map(json.loads, response.content.splitlines()))
    manifest = json.loads(base64.b64decode(response.headers['X-ODIN-Audit-Manifest']))
    assert manifest['known_breaks']


def test_walk_reports_new_break_after_recorded_corruption_and_alerts(engine, tier_license):
    tier_license('enterprise'); append(engine, 4)
    with engine.begin() as c:
        c.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) VALUES(99,'owner','x','admin',TRUE)"))
        c.execute(text("UPDATE audit_logs SET action='bad' WHERE id=1"))
    check_daily(engine, force=True)
    with engine.begin() as c:
        c.execute(text("UPDATE audit_logs SET action='new bad' WHERE id=4"))
    result = check_daily(engine, force=True)
    assert result['first_break'] == 1
    assert {b['row_id'] for b in result['breaks']} == {1, 4}
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM alerts')).scalar_one() == 2
    check_daily(engine, force=True)
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM alerts')).scalar_one() == 2


def test_invalid_before_checkpoint_continues_to_later_break(engine):
    append(engine, 3)
    with Session(engine) as db:
        db.execute(text("UPDATE audit_logs SET action='bad' WHERE id=1"))
        assert retain(db, 'id=1 AND timestamp<:cutoff', '2099-01-01') == 1
        db.execute(text("UPDATE audit_logs SET action='later bad' WHERE id=3"))
        result = verify(db.connection())
        assert any(b['reason'] == 'checkpoint_content' and b['known'] for b in result['breaks'])
        assert any(b['row_id'] == 3 and not b['known'] for b in result['breaks'])


def test_corrupt_erasure_is_known_and_can_be_exported(client, engine):
    append(engine)
    with Session(engine) as db:
        db.execute(text("UPDATE audit_logs SET action='bad' WHERE id=1"))
        rewrite(db, 'UPDATE audit_logs SET ip_address=NULL WHERE id=:id', {'id': 1})
        db.commit()
        result = verify(db.connection())
        assert not result['ok'] and all(b['known'] for b in result['breaks'])
    assert client[0].get('/export/audit-logs/jsonl?date_from=2000-01-01&date_to=2099-01-01').status_code == 200


@pytest.mark.parametrize('kind', ['retention', 'erasure'])
def test_heavy_target_uses_bounded_binds(engine, kind):
    with engine.begin() as c:
        c.execute(text("INSERT INTO audit_logs(action,ip_address) VALUES('heavy','192.0.2.1')"), [{}]*1200)
    with Session(engine) as db:
        raw = db.connection().connection.driver_connection
        old = raw.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 512)
        try:
            if kind == 'retention':
                assert retain(db, 'timestamp<:cutoff', '2099-01-01') == 1200
            else:
                rewrite(db, 'UPDATE audit_logs SET ip_address=NULL WHERE id<=:id', {'id': 1200})
            db.commit()
            assert verify(db.connection())['ok']
        finally:
            raw.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, old)


def test_hmac_key_rotation_preserves_old_receipts_and_jwt_independence(engine, monkeypatch):
    from core.audit_keys import rotate, directory
    with engine.connect() as c:
        first = seal({'receipt': 1}, c)
    monkeypatch.setenv('JWT_SECRET_KEY', 'a-different-jwt-key')
    assert authentic(first)
    rotate()
    second = seal({'receipt': 2})
    assert first['key_id'] != second['key_id']
    assert authentic(first) and authentic(second)
    path = directory() / '.audit-hmac-keys.json'
    assert path.stat().st_mode & 0o777 == 0o600
    with engine.connect() as c:
        for key in json.loads(path.read_text())['keys'].values():
            assert key.encode() not in __import__('pathlib').Path(engine.url.database).read_bytes()


@pytest.mark.parametrize('attack', ['delete_status', 'forge_status', 'rewrite_chain'])
def test_external_head_and_status_loss_detected(engine, tier_license, attack):
    from core.audit_integrity import row, content_hash, GENESIS
    tier_license('enterprise'); append(engine, 2)
    check_daily(engine, force=True)
    with engine.begin() as c:
        if attack == 'delete_status':
            c.execute(text('DELETE FROM audit_integrity_status'))
        elif attack == 'forge_status':
            c.execute(text("UPDATE audit_integrity_status SET result='{}'"))
        else:
            previous = GENESIS
            for row_id in (1, 2):
                c.execute(text("UPDATE audit_logs SET action='rewritten',prev_hash=:p WHERE id=:id"), {'p': previous, 'id': row_id})
                previous = content_hash(row(c, row_id))
                c.execute(text('UPDATE audit_logs SET row_hash=:h WHERE id=:id'), {'h': previous, 'id': row_id})
            c.execute(text('UPDATE audit_chain_state SET head_hash=:h'), {'h': previous})
        result = verify(c)
        assert not result['ok']
        assert any(b['reason'] == {'delete_status': 'status_missing', 'forge_status': 'status_unauthentic',
                                 'rewrite_chain': 'signed_head_missing'}[attack] for b in result['breaks'])
    if attack != 'rewrite_chain':
        assert health(engine)['status'] == 'attention'


def test_retention_preserves_external_head_inside_deleted_range(engine, tier_license):
    tier_license('enterprise'); append(engine, 2)
    check_daily(engine, force=True)
    append(engine, 2)
    with Session(engine) as db:
        assert retain(db, 'timestamp<:cutoff', '2099-01-01') == 4
        db.commit()
        assert verify(db.connection())['ok']


def test_daily_cross_worker_lock(engine, tier_license):
    from core.audit_keys import file_lock
    tier_license('enterprise')
    with file_lock('.audit-daily.lock'):
        assert check_daily(engine, force=True) is None
    assert check_daily(engine, force=True)['ok']


def test_siem_udf_has_no_file_reads_and_fails_closed(engine, tier_license, monkeypatch):
    import license_manager as lm
    from core.audit_integrity import _siem_licensed
    tier_license('enterprise'); destination(engine)
    monkeypatch.setattr(lm, 'get_license', lambda: pytest.fail('trigger read license files'))
    append(engine)
    assert _siem_licensed() == 1
    class Broken:
        def has_feature(self, feature):
            raise RuntimeError('unavailable')
    monkeypatch.setattr(lm, '_cached_license', Broken())
    append(engine)
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM audit_siem_outbox')).scalar_one() == 2


def test_gap_is_explicitly_chained_without_recursive_triggers(engine, tier_license, monkeypatch):
    from core.schema.audit_integrity_version import MODULE
    import importlib
    migration = importlib.import_module(MODULE)
    tier_license('enterprise'); destination(engine)
    with engine.begin() as c:
        c.exec_driver_sql('PRAGMA recursive_triggers=OFF')
        c.execute(text("INSERT INTO audit_siem_outbox(destination_id,audit_id) VALUES('owned',:id)"),
                  [{'id': -i-1} for i in range(migration.OUTBOX_LIMIT)])
    append(engine)
    with engine.connect() as c:
        assert c.execute(text("SELECT chain_seq FROM audit_logs WHERE action='siem.gap'")).scalar_one() > 0
        assert verify(c)['ok']


def test_siem_sweeper_refreshes_shared_sqlite_license_deadline(engine, tier_license):
    from modules.system.audit_siem import licensed_destinations
    tier_license('enterprise')
    writes = []
    def observe(c, cursor, sql, params, ctx, many):
        if 'siem_until' in sql:
            writes.append(sql)
    event.listen(engine, 'before_cursor_execute', observe)
    try:
        licensed_destinations(engine)
    finally:
        event.remove(engine, 'before_cursor_execute', observe)
    assert len(writes) == 1


@pytest.mark.parametrize('tier,days,expected', [('enterprise', 30, 90), ('enterprise', 0, 0),
    ('enterprise', 90, 90), ('pro', 30, 30)])
def test_retention_floor(tier_license, tier, days, expected):
    from core.audit_retention_policy import effective_retention, validate_retention
    from fastapi import HTTPException
    tier_license(tier)
    assert effective_retention({'audit_logs_days': days})['audit_logs_days'] == expected
    if days != expected:
        with pytest.raises(HTTPException, match='90'):
            validate_retention({'audit_logs_days': days})
    else:
        validate_retention({'audit_logs_days': days})


def test_retention_api_enforces_floor_after_license_upgrade(client, engine):
    from datetime import datetime, timedelta, timezone
    from modules.system.routes_config import router
    http, _ = client
    http.app.include_router(router)
    with engine.begin() as c:
        c.execute(text("INSERT INTO system_config(key,value) VALUES('data_retention',:v)"),
                  {'v': json.dumps({'audit_logs_days': 1})})
        for days in (30, 100):
            c.execute(text("INSERT INTO audit_logs(timestamp,action) VALUES(:t,'expiry')"),
                      {'t': (datetime.now(timezone.utc)-timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')})
    assert http.get('/config/retention').json()['audit_logs_days'] == 90
    assert http.put('/config/retention', json={'audit_logs_days': 89}).status_code == 400
    response = http.post('/admin/retention/cleanup')
    assert response.status_code == 200, response.text
    assert response.json()['deleted']['audit_logs'] == 1
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM audit_logs WHERE action='expiry'")).scalar_one() == 1
        assert verify(c)['ok']
