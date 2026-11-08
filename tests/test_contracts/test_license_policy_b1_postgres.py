"""B1 real PostgreSQL CAS, vacancy and recovery transaction parity."""
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_controlled_postgres import native_pg


def test_postgres_selection_concurrency_vacancy_and_handoff(native_pg, tier_license, monkeypatch):
    from core.schema import bootstrap_database
    from core.auth import hash_password
    from core import license_lock as policy
    bootstrap_database(native_pg)
    with native_pg.begin() as c:
        c.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) VALUES(1,'one',:h,'admin',TRUE),(2,'two',:h,'admin',TRUE)"), {'h': hash_password('SyntheticPass123!')})
        c.execute(text("INSERT INTO printers(id,name,is_active) VALUES(1,'one',TRUE),(2,'two',TRUE)"))
    tier_license('enterprise', max_users=1, max_printers=1)
    monkeypatch.setenv('ODIN_LICENSE_FLEET_LOCK_ENABLED', 'true')
    with Session(native_pg) as db:
        initial = policy.reconcile(db); db.commit()
    def save(number):
        with Session(native_pg) as db:
            try:
                p = policy.replace_selection(db, users=[number], printers=[number], recovery_user_id=number,
                                             expected_revision=initial['revision'], actor=1)
                db.commit(); return 200, p
            except policy.LicenseLockError as exc:
                db.rollback(); return exc.status_code, exc.detail
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(save, [1, 2]))
    assert sorted(r[0] for r in result) == [200, 409]
    winner = next(p for code, p in result if code == 200)
    with Session(native_pg) as db:
        db.execute(text('UPDATE printers SET is_active=FALSE WHERE id=:id'), {'id': winner['printers'][0]}); db.commit()
        p = policy.reconcile(db); db.commit()
        assert p['printers'] != winner['printers']
        assert p['membership_generation'] > winner['membership_generation']
        db.execute(text("UPDATE users SET password_hash='' WHERE id=:id"), {'id': 3 - winner['users'][0]}); db.commit()
        db.execute(text('DELETE FROM users WHERE id=:id'), {'id': winner['users'][0]})
        with pytest.raises(policy.LicenseLockError): db.commit()
        db.rollback()
