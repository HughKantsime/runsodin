"""Owned native PostgreSQL proof of post-027 migrations and preserved029 historical restores.

Live source on PYTHONPATH. Archives of each era are written by the preserved029 sources
(post027_archive_fixtures.py). Prints post027-native-* facts; the last is post027-native-pass.
"""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import uuid


def main():
    with tempfile.TemporaryDirectory(prefix='post027-owned-') as folder:
        root = Path(folder); root.chmod(0o700)
        credential = root / 'credential'; credential.write_text(secrets.token_urlsafe(48)); credential.chmod(0o600)
        maintenance_credential = root / 'maintenance'; maintenance_credential.write_text(secrets.token_urlsafe(48)); maintenance_credential.chmod(0o600)
        raw = os.environ['POST027_NATIVE_URL']
        maintenance = raw.replace('postgresql+psycopg://', 'postgresql://')
        server = maintenance.rsplit('/', 1)[0]
        target = server + '/post027_target'
        os.environ.update(DATABASE_URL=target, DATABASE_PASSWORD_FILE=str(credential), ODIN_DATA_DIR=folder, JWT_SECRET_KEY=secrets.token_urlsafe(48))
        from sqlalchemy import create_engine, inspect, text
        import core
        assert '/preserved029/' not in core.__file__
        from core.database_config import create_database_engine
        from core.schema import bootstrap_database
        from core.schema.bootstrap import POST027_MIGRATIONS
        from core.schema import preserved029
        from modules.system import postgres_backup_service as b
        from modules.system.backup_service import BackupValidationError
        from modules.system.postgres_canonical_executables import validate_executables
        admin = create_engine(raw, isolation_level='AUTOCOMMIT', hide_parameters=True)
        post_ids = [migration_id for migration_id, _ in POST027_MIGRATIONS]
        from core.quiet_hours_deferral_schema import TABLE as DEFERRED, EXPECTED_COLUMNS
        from core.quiet_hours_deferral_schema import validate as validate_032

        def fresh(name):
            with admin.connect() as c:
                c.exec_driver_sql('DROP DATABASE IF EXISTS ' + name + ' WITH (FORCE)'); c.exec_driver_sql('CREATE DATABASE ' + name)
            return create_database_engine(server + '/' + name, role='restore', password_file=str(credential))

        def has_index(engine):
            # Migration 032's table, exactly shaped (name kept from the 030 proof's helper).
            with engine.connect() as c:
                validate_032(c)
                return {r[0] for r in c.execute(text("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=:t"), {'t': DEFERRED})} == EXPECTED_COLUMNS

        # 1. Fresh PostgreSQL boot, twice.
        boot = fresh('post027_fresh')
        first = bootstrap_database(boot); second = bootstrap_database(boot)
        assert first['applied'][-2:] == post_ids[-1:] + ['orm-baseline:1'] and second['applied'] == []
        assert second['schema_fingerprint'] == first['schema_fingerprint'] and has_index(boot)
        with boot.connect() as c:validate_executables(c)
        boot.dispose()
        print('post027-native-fresh-boot-twice-032-applied-once-fingerprint-stable-pass', flush=True)

        # 2. Archives of each era from the preserved sources.
        archives = root / 'archives'; archives.mkdir()
        bundle, _ = preserved029.integrity.check_package()
        made = subprocess.run([sys.executable, str(Path(__file__).with_name('post027_archive_fixtures.py')), raw, server, str(credential), str(archives)],
                              env={**os.environ, 'PYTHONPATH': str(bundle), 'PYTHONDONTWRITEBYTECODE': '1'}, capture_output=True, text=True, timeout=1800)
        assert made.returncode == 0, made.stderr[-3000:]
        facts = {int(k): v for k, v in json.loads(made.stdout.strip().splitlines()[-1]).items()}
        assert not set(post_ids) & set(facts[27]['ledger']) and len(facts[27]['ledger']) == 52
        assert len(facts[25]['ledger']) == 50 and len(facts[26]['ledger']) == 51
        print('post027-native-preserved-sources-wrote-real-025-026-027-archives-pass', flush=True)

        # 3. Target at the current schema, then validate, stage and apply each historical archive.
        engine = fresh('post027_target'); bootstrap_database(engine)
        with engine.begin() as c:c.execute(text("INSERT INTO system_config(key,value) VALUES('historical_marker','\"target\"')"))

        def marker():
            # system_config.value is a JSON column: the driver returns the decoded string.
            with engine.connect() as c:return c.execute(text("SELECT value FROM system_config WHERE key='historical_marker'")).scalar_one()

        args = {'password_file': str(credential), 'maintenance_password_file': str(maintenance_credential), 'base_dir': root}
        # A non-credential libpq parameter on the maintenance URL must not break historical restores.
        maintenance = maintenance + '?sslmode=disable'
        b.postgres_paths(root).backups.mkdir(parents=True, exist_ok=True)

        def leftover():
            with admin.connect() as c:
                return c.execute(text("SELECT count(*) FROM pg_database WHERE datname LIKE 'odin_validate_%'")).scalar_one()

        for era in (25, 26, 27):
            archive = Path(facts[era]['archive'])
            before = marker()
            metadata = b.validate_postgres_restore(archive, target, maintenance, **args)
            assert metadata['sha256'] == facts[era]['sha256'] and metadata['source_schema_version'] == era
            assert metadata['prepared_schema_version'] == int(post_ids[-1].split(':')[1][:3]) and metadata['prepared_sha256'] != facts[era]['sha256']
            assert marker() == before and b._sha256(archive) == facts[era]['sha256'] and leftover() == 0
            staged = b.stage_postgres_restore(archive, target, maintenance, **args)
            assert staged['candidate_sha256'] == facts[era]['sha256']
            engine.dispose()
            result = b.apply_pending_postgres_restore(target, maintenance, acknowledgement=facts[era]['sha256'], **args)
            assert result['status'] == 'restored' and marker() == f'source{era}'
            with engine.connect() as c:
                ids = {row[0] for row in c.execute(text('SELECT migration_id FROM odin_schema_migrations'))}
                assert set(facts[era]['ledger']) <= ids and set(post_ids) <= ids
                assert c.execute(text('SELECT count(*) FROM filament_library WHERE id IN (80,81)')).scalar_one() == 2
                validate_executables(c)
            assert has_index(engine) and leftover() == 0
            assert not list(b.postgres_paths(root).backups.glob('.odin-upgraded029-*'))
            print(f'post027-native-era{era}-archive-preserved029-stage-live-upgrade-validated-staged-applied-data-ledger-032-sslmode-query-pass', flush=True)

        # 4. Refusals: a changed 027-era ledger and an extra table are refused, target untouched.
        before = marker()
        for path, expected in ((Path(facts[27]['tampered']), 'Migration history mismatch: python:027-power-target-operations'),
                               (Path(facts[26]['tampered']), 'Unsupported PostgreSQL archive schema version or partial table inventory')):
            try:b.validate_postgres_restore(path, target, maintenance, **args)
            except BackupValidationError as error:
                import re
                assert re.search(expected, str(error)), str(error)
            else:raise AssertionError('Tampered historical archive admitted: ' + path.name)
            assert marker() == before and leftover() == 0
        print('post027-native-tampered-checksum-and-extra-table-refused-target-untouched-no-stage-left-pass', flush=True)

        # 5. Current-schema backup -> restore round trip keeps data, including a 032 row.
        with engine.begin() as c:
            c.execute(text("UPDATE system_config SET value='\"current-backup\"' WHERE key='historical_marker'"))
            c.execute(text("INSERT INTO " + DEFERRED + "(id,user_id,org_id,scope_key,channel,alert_type,severity,title,state) "
                           "VALUES('roundtrip032',3,1,'org:1','email','print_failed','critical','kept','pending')"))
        backup, backup_metadata = b.create_postgres_backup(target, maintenance_url=maintenance, **args)
        with engine.begin() as c:
            c.execute(text("UPDATE system_config SET value='\"after-backup\"' WHERE key='historical_marker'"))
            c.execute(text("DELETE FROM " + DEFERRED))
        current = b.validate_postgres_restore(backup, target, maintenance, **args)
        assert 'source_schema_version' not in current and current['restore_validated']
        b.stage_postgres_restore(backup, target, maintenance, **args); engine.dispose()
        result = b.apply_pending_postgres_restore(target, maintenance, acknowledgement=backup_metadata['sha256'], **args)
        assert result['status'] == 'restored' and marker() == 'current-backup' and has_index(engine)
        with engine.connect() as c:
            validate_executables(c)
            assert c.execute(text("SELECT id,title FROM " + DEFERRED)).all() == [('roundtrip032', 'kept')]
        print('post027-native-current-backup-restore-roundtrip-data-and-032-row-pass', flush=True)

        engine.dispose()

        # 6. A database written by the published v1.9.24 image's own bootstrap (run by the
        #    host runner) upgrades in place, then backs up and restores with this source.
        released = create_database_engine(server + '/post027_released', role='restore', password_file=str(credential))
        with released.connect() as c:
            assert c.execute(text("SELECT checksum FROM odin_schema_migrations WHERE migration_id='orm-baseline:1'")).scalar_one().startswith('36d66151')
        upgraded = bootstrap_database(released); again = bootstrap_database(released)
        assert post_ids[-1] in upgraded['applied'] and again['applied'] == [] and again['schema_fingerprint'] == upgraded['schema_fingerprint']
        fresh_boot = create_database_engine(server + '/post027_fresh', role='restore', password_file=str(credential))
        def order(engine, table):
            with engine.connect() as c:
                return [r[0] for r in c.execute(text("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=:t ORDER BY ordinal_position"), {'t': table})]
        reordered = [t for t in ('jobs', 'printers') if order(released, t) != order(fresh_boot, t) and sorted(order(released, t)) == sorted(order(fresh_boot, t))]
        assert reordered, 'v1.9.24 upgrade did not reproduce the column-order difference'
        fresh_boot.dispose()
        released_url = server + '/post027_released'
        b.validate_live_postgres_objects(released_url, password_file=str(credential))
        with released.begin() as c:c.execute(text("UPDATE system_config SET value='\"released-backup\"' WHERE key='historical_marker'"))
        backup, backup_metadata = b.create_postgres_backup(released_url, maintenance_url=maintenance, **args)
        with released.begin() as c:c.execute(text("UPDATE system_config SET value='\"after\"' WHERE key='historical_marker'"))
        b.stage_postgres_restore(backup, released_url, maintenance, **args); released.dispose()
        result = b.apply_pending_postgres_restore(released_url, maintenance, acknowledgement=backup_metadata['sha256'], **args)
        with released.connect() as c:
            assert result['status'] == 'restored' and c.execute(text("SELECT value FROM system_config WHERE key='historical_marker'")).scalar_one() == 'released-backup'
            validate_executables(c)
        assert has_index(released); released.dispose()
        print('post027-native-published-v1.9.24-database-upgraded-reordered-' + '-'.join(reordered) + '-backup-validated-restored-pass', flush=True)

        # 7. Apply failures: before the restore body (its rollback dump is removed), and inside
        #    it after the rollback (the archive is kept with a record and a superadmin notice).
        from contextlib import contextmanager
        from modules.system import pre026_restore_stage as stage_module
        from modules.system import routes_backup
        archive27 = Path(facts[27]['archive'])
        rollbacks = lambda: sorted(p.name for p in b.postgres_paths(root).backups.glob('rollback_*.dump'))
        b.stage_postgres_restore(archive27, target, maintenance, **args); engine.dispose()
        before_rollbacks = rollbacks()
        real_prepared = stage_module.prepared_archive
        @contextmanager
        def refusing_prepared(*a, **k):
            raise b.ArchiveRefused('synthetic refusal after the rollback dump')
            yield
        stage_module.prepared_archive = refusing_prepared
        try:
            try:b.apply_pending_postgres_restore(target, maintenance, acknowledgement=facts[27]['sha256'], **args)
            except b.ArchiveRefused:pass
            else:raise AssertionError('refusal did not propagate')
        finally:
            stage_module.prepared_archive = real_prepared
        assert rollbacks() == before_rollbacks and b.postgres_paths(root).manifest.exists()
        before = marker()
        try:b.apply_pending_postgres_restore(target, maintenance, acknowledgement=facts[27]['sha256'], fail_after_restore=True, **args)
        except RuntimeError as error:assert 'Injected post-restore' in str(error)
        else:raise AssertionError('post-restore failure did not fire')
        assert marker() == before and not b.postgres_paths(root).manifest.exists()
        kept = [r for r in b.quarantined_restores(root) if r['archive']]
        assert kept and 'rolled back' in kept[0]['reason'], kept
        notices = routes_backup.active_system_notices()
        assert notices and notices[0]['archive'] == kept[0]['archive'], notices
        for path in b.postgres_paths(root).backups.glob('failed_restore_*'):path.unlink()
        print('post027-native-failed-attempt-removes-its-rollback-dump-rolled-back-apply-keeps-archive-and-notice-pass', flush=True)

        # 8. A preserved029 child killed mid-run leaves no validation database or working file.
        import signal, threading
        from modules.system import restore_artifacts
        journal = restore_artifacts.journal_dir(b.postgres_paths(root).backups)
        real_popen = preserved029.subprocess.Popen
        killed = []
        def killing_popen(argv, **kwargs):
            process = real_popen(argv, **kwargs)
            # The child journals into its own subdirectory: watch every journal file.
            before = {str(entry) for entry in journal.rglob('*') if entry.is_file()}
            def watch():
                while process.poll() is None:
                    if {str(entry) for entry in journal.rglob('*') if entry.is_file()} - before:
                        os.killpg(process.pid, signal.SIGKILL); killed.append(True); return
                    __import__('time').sleep(.05)
            threading.Thread(target=watch, daemon=True).start()
            return process
        preserved029.subprocess.Popen = killing_popen
        try:
            try:b.validate_postgres_restore(Path(facts[26]['archive']), target, maintenance, **args)
            except BackupValidationError as error:assert 'did not validate' in str(error) or 'refused' in str(error), str(error)
            else:raise AssertionError('Killed child admitted')
        finally:
            preserved029.subprocess.Popen = real_popen
        assert killed and leftover() == 0 and not restore_artifacts.journal_entries(b.postgres_paths(root).backups)
        assert not list(b.postgres_paths(root).backups.glob('.odin-preserved029-*')) and not list(b.postgres_paths(root).backups.glob('.odin-*.dump'))
        print('post027-native-killed-child-no-stage-or-validation-database-or-dump-left-pass', flush=True)

        preserved029.integrity.check_package()
        with admin.connect() as c:
            assert c.execute(text("SELECT count(*) FROM pg_database WHERE datname LIKE 'odin_validate_%'")).scalar_one() == 0
            for name in ('post027_target', 'post027_fresh', 'post027_released'):c.exec_driver_sql('DROP DATABASE IF EXISTS ' + name + ' WITH (FORCE)')
        admin.dispose()
    print('post027-native-pass', flush=True)


if __name__ == '__main__':
    main()
