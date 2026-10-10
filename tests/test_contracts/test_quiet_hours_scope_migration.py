"""Historical digest claims survive scope upgrade without allowing resends."""
import importlib
import pytest
from pathlib import Path
from sqlalchemy import text,inspect
from tests.test_contracts.test_education_review_workflow import review_db


def _restore_legacy_tables(engine):
    from core.schema.migrator import split_sql_statements
    root=Path(__file__).resolve().parents[2]/'backend/core/migrations'
    with engine.begin() as conn:
        for table in ('quiet_hours_digest_sends','quiet_hours_org_digest_sends'):
            conn.execute(text(f'DROP TABLE {table}'))
        for filename in ('002_quiet_hours_digest_sends.sql','003_quiet_hours_org_digest_sends.sql'):
            for statement in split_sql_statements((root/filename).read_text()):
                conn.execute(text(statement))
        for table in ('quiet_hours_digest_sends','quiet_hours_org_digest_sends'):
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN delivery_status TEXT DEFAULT 'pending'"))
        conn.execute(text("DELETE FROM odin_schema_migrations WHERE migration_id='python:004-quiet-hours-scoped-claims'"))


def test_claim_upgrade_preserves_history_and_allows_distinct_new_scopes(review_db):
    migration=importlib.import_module('core.schema.migrations.004_quiet_hours_scoped_claims')
    engine=review_db.get_bind()
    _restore_legacy_tables(engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO quiet_hours_digest_sends(id,user_id,org_id,window_ended_at,delivery_status) VALUES(1,3,1,'historical','failed:email')"))
        conn.execute(text("INSERT INTO quiet_hours_org_digest_sends(id,org_id,window_ended_at,delivery_status) VALUES(1,NULL,'historical','sent'),(2,NULL,'historical','failed:webhook')"))
        migration.apply(conn);migration.validate(conn)
        row=conn.execute(text('SELECT user_id,org_id,scope_key,window_ended_at,delivery_status FROM quiet_hours_digest_sends WHERE id=1')).one()
        assert tuple(row)==(3,1,'legacy','historical','failed:email')
        orgs=conn.execute(text('SELECT id,scope_key,delivery_status FROM quiet_hours_org_digest_sends ORDER BY id')).all()
        assert [tuple(r) for r in orgs]==[(1,'legacy:1','sent'),(2,'legacy:2','failed:webhook')]
        for scope in ['system','org:1','org:2']:
            conn.execute(text("INSERT INTO quiet_hours_digest_sends(user_id,org_id,scope_key,window_ended_at) VALUES(3,NULL,:scope,'new')"),{'scope':scope})
    with engine.begin() as conn:
        before=conn.execute(text('SELECT count(*) FROM quiet_hours_digest_sends')).scalar_one()
        migration.apply(conn);migration.validate(conn)
        assert conn.execute(text('SELECT count(*) FROM quiet_hours_digest_sends')).scalar_one()==before==4


def test_scope_upgrade_failure_rolls_back_every_table(review_db,monkeypatch):
    import pytest
    migration=importlib.import_module('core.schema.migrations.004_quiet_hours_scoped_claims')
    engine=review_db.get_bind()
    _restore_legacy_tables(engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO quiet_hours_digest_sends(user_id,org_id,window_ended_at,delivery_status) VALUES(3,1,'preserve','sent')"))
    original=migration.text
    def fail_second_table(statement):
        if 'CREATE TABLE quiet_hours_org_digest_sends_scoped_upgrade' in statement:
            raise RuntimeError('Synthetic interruption')
        return original(statement)
    monkeypatch.setattr(migration,'text',fail_second_table)
    with pytest.raises(RuntimeError,match='Synthetic interruption'):
        from core.schema.migrator import apply_python_migration
        with engine.begin() as conn:apply_python_migration(conn,"core.schema.migrations.004_quiet_hours_scoped_claims")
    with engine.connect() as conn:
        assert 'scope_key' not in {c['name'] for c in inspect(conn).get_columns('quiet_hours_digest_sends')}
        assert not any(name.endswith('_scoped_upgrade') for name in inspect(conn).get_table_names())
        assert conn.execute(text("SELECT delivery_status FROM quiet_hours_digest_sends WHERE window_ended_at='preserve'")).scalar_one()=='sent'
    monkeypatch.setattr(migration,'text',original)
    with engine.begin() as conn:migration.apply(conn);migration.validate(conn)


@pytest.mark.parametrize('change',['scope_type','window_nullable','user_type','no_primary','status_default','extra_unique'])
def test_scoped_adoption_rejects_incompatible_shape(change,review_db):
    from core.schema.migrator import MigrationError
    migration=importlib.import_module('core.schema.migrations.004_quiet_hours_scoped_claims')
    definitions={'id':'INTEGER PRIMARY KEY AUTOINCREMENT','user_id':'INTEGER NOT NULL',
        'org_id':'INTEGER','scope_key':'TEXT NOT NULL','window_ended_at':'TEXT NOT NULL',
        'sent_at':'TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP','delivery_status':"TEXT DEFAULT 'pending'"}
    if change=='scope_type':definitions['scope_key']='INTEGER NOT NULL'
    elif change=='window_nullable':definitions['window_ended_at']='TEXT'
    elif change=='user_type':definitions['user_id']='TEXT NOT NULL'
    elif change=='no_primary':definitions['id']='INTEGER'
    elif change=='status_default':definitions['delivery_status']="TEXT DEFAULT 'sent'"
    extra=',UNIQUE(user_id,window_ended_at)' if change=='extra_unique' else ''
    with review_db.get_bind().begin() as conn:
        conn.execute(text('DROP TABLE quiet_hours_digest_sends'))
        cols=','.join(f'{name} {definition}' for name,definition in definitions.items())
        conn.execute(text(f'CREATE TABLE quiet_hours_digest_sends({cols},UNIQUE(user_id,scope_key,window_ended_at){extra})'))
        conn.execute(text('CREATE INDEX idx_qhds_window_ended_at ON quiet_hours_digest_sends(window_ended_at)'))
        conn.execute(text('CREATE INDEX idx_qhds_org_id ON quiet_hours_digest_sends(org_id)'))
        with pytest.raises((MigrationError,RuntimeError)):
            migration.apply(conn);migration.validate(conn)
