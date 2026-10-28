"""Alert enum ledger, upgrade preservation, and native PostgreSQL compatibility."""
import importlib
import pytest
from sqlalchemy import create_engine, text
from core.schema import bootstrap_database
from core.schema.migrator import MigrationError

M = importlib.import_module('core.schema.migrations.045_pause_cancel_alerts')


@pytest.fixture
def engine(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path/'alerts.db'))
    yield engine
    engine.dispose()


def test_fresh_repeat_and_pinned_enum_matches_model(engine):
    from core.base import AlertType
    assert M.CURRENT == tuple(item.value for item in AlertType)
    first = bootstrap_database(engine)
    assert M.MIGRATION_ID in first['applied']
    second = bootstrap_database(engine)
    assert second['applied'] == [] and first['schema_fingerprint'] == second['schema_fingerprint']
    with engine.begin() as c:
        M.apply(c)
        M.validate(c)


def test_upgrade_preserves_alerts_and_preferences(engine):
    bootstrap_database(engine)
    with engine.begin() as c:
        c.execute(text('DELETE FROM odin_schema_migrations WHERE migration_id=:id'), {'id': M.MIGRATION_ID})
        c.execute(text("INSERT INTO alert_preferences(user_id,alert_type,in_app) VALUES(1,'print_failed',FALSE)"))
        c.execute(text("INSERT INTO alerts(user_id,alert_type,severity,title) VALUES(1,'print_failed','critical','Existing alert')"))
    assert bootstrap_database(engine)['applied'] == [M.MIGRATION_ID]
    with engine.connect() as c:
        assert c.execute(text('SELECT title FROM alerts')).scalar_one() == 'Existing alert'
        assert c.execute(text('SELECT in_app FROM alert_preferences')).scalar_one() == 0
    assert bootstrap_database(engine)['applied'] == []


def test_drift_refused_without_migration_ledger(engine):
    bootstrap_database(engine)
    with engine.begin() as c:
        c.execute(text('DELETE FROM odin_schema_migrations WHERE migration_id=:id'), {'id': M.MIGRATION_ID})
        c.exec_driver_sql('ALTER TABLE alert_preferences RENAME COLUMN alert_type TO wrong_name')
    with pytest.raises(MigrationError):
        bootstrap_database(engine)
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM odin_schema_migrations WHERE migration_id=:id'), {'id': M.MIGRATION_ID}).scalar_one() == 0


def test_published_1924_entrypoint_upgrade(tmp_path):
    from tests.test_contracts.test_post027_migration_032 import test_published_v1_9_24_database_upgrades_through_032_with_the_entrypoint_command
    test_published_v1_9_24_database_upgrades_through_032_with_the_entrypoint_command(tmp_path)
    engine = create_engine('sqlite:///' + str(tmp_path/'odin.db'))
    try:
        with engine.connect() as c:
            M.validate(c)
            assert c.execute(text('SELECT count(*) FROM odin_schema_migrations WHERE migration_id=:id'), {'id': M.MIGRATION_ID}).scalar_one() == 1
    finally:
        engine.dispose()


from tests.test_contracts.test_post027_migration_040 import pg_engine  # noqa: E402,F401


def test_postgres_legacy_enum_upgrade_then_delivery_preferences(pg_engine):
    from core.alert_preferences import preferences_for, seed_attention_preferences
    bootstrap_database(pg_engine)
    with pg_engine.begin() as c:
        c.exec_driver_sql('ALTER TYPE public.alerttype RENAME TO alerttype_current_test')
        c.exec_driver_sql('CREATE TYPE public.alerttype AS ENUM (' + ','.join("'"+value+"'" for value in M.LEGACY) + ')')
        for table in ('alerts', 'alert_preferences'):
            c.exec_driver_sql(f'ALTER TABLE {table} ALTER COLUMN alert_type TYPE public.alerttype USING alert_type::text::public.alerttype')
        c.exec_driver_sql('DROP TYPE public.alerttype_current_test')
        c.execute(text('DELETE FROM odin_schema_migrations WHERE migration_id=:id'), {'id': M.MIGRATION_ID})
        c.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) VALUES(901,'pause-test','unused','admin',TRUE)"))
        c.execute(text("INSERT INTO alert_preferences(user_id,alert_type,in_app) VALUES(901,'print_failed',FALSE)"))
    assert bootstrap_database(pg_engine)['applied'] == [M.MIGRATION_ID]
    assert bootstrap_database(pg_engine)['applied'] == []
    with pg_engine.begin() as c:
        seed_attention_preferences(c)
        seed_attention_preferences(c)
        assert preferences_for(c, 'print_failed')[0].in_app is False
        for kind in ('print_paused', 'print_cancelled'):
            pref, = preferences_for(c, kind)
            assert pref.user_id == 901
            assert pref.in_app is True and pref.browser_push is True and pref.email is False
            assert c.execute(text('SELECT count(*) FROM alert_preferences WHERE alert_type=:kind'),
                             {'kind': kind}).scalar_one() == 1
            c.execute(text('INSERT INTO alerts(user_id,alert_type,severity,title) VALUES(901,:kind,\'critical\',\'New alert\')'), {'kind': kind})
        c.execute(text("UPDATE alert_preferences SET in_app=FALSE,browser_push=FALSE,email=TRUE "
                       "WHERE user_id=901 AND alert_type='print_paused'"))
    # Runtime dispatch also uses a Session; reseeding must retain the saved opt-out.
    from sqlalchemy.orm import Session
    with Session(pg_engine) as db:
        seed_attention_preferences(db)
        paused, = preferences_for(db, 'print_paused')
        assert paused.in_app is False and paused.browser_push is False and paused.email is True
        assert db.execute(text("SELECT count(*) FROM alert_preferences WHERE alert_type='print_paused'")).scalar_one() == 1
