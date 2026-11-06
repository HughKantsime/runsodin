"""Owned PostgreSQL lock bounds; runs wherever the native fixture is available."""
import time
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from core.schema import bootstrap_database
from modules.organizations.education_training_policy import lock_center
from modules.organizations.education_submission_graph import _lock_job_birth_authority
from tests.test_contracts.test_post027_migration_033_postgres import pg_engine


def test_training_and_upload_authority_waits_are_bounded(pg_engine):
    bootstrap_database(pg_engine)
    with pg_engine.begin() as c:
        c.execute(text("INSERT INTO groups(id,name,is_org) VALUES(1,'School',TRUE)"))
        c.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES(1,'student','fixture','viewer',TRUE,1)"))
        c.execute(text("INSERT INTO education_cost_centers(id,org_id,name_key,code_key,display_name,code,created_by) VALUES(7,1,'class','class','Class','CLASS',1)"))
    with pg_engine.connect() as holder, Session(pg_engine) as waiting:
        holder.execute(text('UPDATE education_cost_centers SET revision=revision WHERE id=7'))
        started = time.monotonic()
        with pytest.raises(OperationalError) as error:
            lock_center(waiting, 7, 1)
        assert error.value.orig.sqlstate == '55P03'
        assert time.monotonic() - started < 2
        waiting.rollback()
        # Upload authorization must establish its bound before taking this same
        # class lock. NOWAIT may refuse immediately; it must never wait unbounded.
        started = time.monotonic()
        with pytest.raises(OperationalError) as error:
            _lock_job_birth_authority(waiting, 'owned-upload', {'id': 1, 'group_id': 1}, 7)
        assert error.value.orig.sqlstate == '55P03'
        assert time.monotonic() - started < 2
        waiting.rollback()
        holder.rollback()


@pytest.mark.parametrize('timeout', ['0', '500ms', '4s'])
def test_successful_training_locks_restore_timeout(pg_engine, timeout):
    bootstrap_database(pg_engine)
    with Session(pg_engine) as session:
        session.execute(text("SELECT set_config('lock_timeout',:value,true)"), {'value': timeout})
        before = session.execute(text('SHOW lock_timeout')).scalar_one()
        lock_center(session, 7, 1)
        assert session.execute(text('SHOW lock_timeout')).scalar_one() == before
        session.rollback()


def test_upload_cap_survives_training_locks_for_later_graph_writes(pg_engine):
    from core.errors import OdinError
    bootstrap_database(pg_engine)
    with pg_engine.begin() as conn:
        conn.execute(text("INSERT INTO groups(id,name,is_org) VALUES(101,'Upload cap school',TRUE)"))
        conn.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES(101,'upload-cap','fixture','viewer',TRUE,101)"))
        conn.execute(text("INSERT INTO education_cost_centers(id,org_id,name_key,code_key,display_name,code,created_by) VALUES(107,101,'cap','cap','Cap','CAP',101)"))
    with pg_engine.connect() as holder, Session(pg_engine) as waiting:
        holder.execute(text("UPDATE users SET username=username WHERE id=101"))
        waiting.execute(text("SET LOCAL lock_timeout = '4s'"))
        # NOWAIT authority refusal leaves the transaction aborted, so use an
        # unlocked identity first and contend on a later graph-like write.
        with pytest.raises(OdinError):
            _lock_job_birth_authority(waiting, 'missing-upload', {'id': 999, 'group_id': 101}, 107)
        assert waiting.execute(text('SHOW lock_timeout')).scalar_one() == '1s'
        started = time.monotonic()
        with pytest.raises(OperationalError) as error:
            waiting.execute(text('UPDATE users SET username=username WHERE id=101'))
        assert error.value.orig.sqlstate == '55P03'
        assert time.monotonic() - started < 2
        waiting.rollback()
        holder.rollback()
