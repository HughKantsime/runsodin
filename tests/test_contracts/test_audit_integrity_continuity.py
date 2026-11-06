"""Continuity metadata and migration index regression checks."""
from sqlalchemy import text
from core.audit_receipts import seal, authentic
from modules.system.audit_integrity_job import check_daily, health
from tests.test_contracts.test_audit_integrity_e1 import engine, append


def test_signed_status_timestamp_must_match_database_column(engine, tier_license):
    tier_license('enterprise'); append(engine)
    check_daily(engine, force=True)
    with engine.begin() as c:
        c.execute(text('UPDATE audit_integrity_status SET checked_at=checked_at+100000'))
    assert health(engine)['status'] == 'attention'


def test_retired_keys_remain_valid_after_process_reload(engine):
    import importlib
    from core import audit_keys
    with engine.connect() as c:
        receipt = seal({'receipt': 'old'}, c)
    audit_keys.rotate()
    importlib.reload(audit_keys)
    assert authentic(receipt)


def test_daily_lock_excludes_a_separate_process(engine, tier_license):
    import os, subprocess, sys
    from core.audit_keys import file_lock
    tier_license('enterprise')
    command = [sys.executable, '-c',
        "from core.audit_keys import file_lock\nwith file_lock('.audit-daily.lock', blocking=False) as acquired: print(int(acquired))"]
    env = {**os.environ, 'PYTHONPATH': os.path.abspath('backend')}
    with file_lock('.audit-daily.lock'):
        assert subprocess.check_output(command, env=env, text=True).strip() == '0'
    assert subprocess.check_output(command, env=env, text=True).strip() == '1'


def test_missing_external_head_cannot_disable_full_walk_check(engine, tier_license):
    from core.audit_keys import directory
    from core.audit_integrity import verify
    tier_license('enterprise'); append(engine)
    check_daily(engine, force=True)
    (directory() / '.audit-signed-head.json').unlink()
    assert health(engine)['status'] == 'attention'
    with engine.begin() as c:
        c.execute(text('DELETE FROM audit_logs'))
        result = verify(c)
    reasons = {b['reason'] for b in result['breaks']}
    assert {'signed_head_unavailable', 'signed_head_missing'} <= reasons


def test_cached_siem_license_checks_expiry_without_file_refresh(engine, tier_license):
    from datetime import datetime, timezone
    from core.audit_integrity import _siem_licensed
    license = tier_license('enterprise')
    assert _siem_licensed() == 1
    license.expiry_deadline = datetime(2000, 1, 1, tzinfo=timezone.utc)
    assert _siem_licensed() == 0
