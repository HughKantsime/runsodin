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
        index = 'ux_spools_spoolman_spool_id'

        def fresh(name):
            with admin.connect() as c:
                c.exec_driver_sql('DROP DATABASE IF EXISTS ' + name + ' WITH (FORCE)'); c.exec_driver_sql('CREATE DATABASE ' + name)
            return create_database_engine(server + '/' + name, role='restore', password_file=str(credential))

        def has_index(engine):
            with engine.connect() as c:
                return c.execute(text("SELECT indexdef FROM pg_indexes WHERE schemaname='public' AND indexname=:n"), {'n': index}).scalar()

        # 1. Fresh PostgreSQL boot, twice.
        boot = fresh('post027_fresh')
        first = bootstrap_database(boot); second = bootstrap_database(boot)
        assert first['applied'][-2:] == post_ids[-1:] + ['orm-baseline:1'] and second['applied'] == []
        assert second['schema_fingerprint'] == first['schema_fingerprint'] and 'WHERE (spoolman_spool_id IS NOT NULL)' in has_index(boot)
        with boot.connect() as c:validate_executables(c)
        boot.dispose()
        print('post027-native-fresh-boot-twice-030-applied-once-fingerprint-stable-pass', flush=True)

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
            print(f'post027-native-era{era}-archive-preserved029-stage-live-upgrade-validated-staged-applied-data-ledger-030-pass', flush=True)

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

        # 5. Current-schema backup -> restore round trip keeps data and the 030 index.
        with engine.begin() as c:c.execute(text("UPDATE system_config SET value='\"current-backup\"' WHERE key='historical_marker'"))
        backup, backup_metadata = b.create_postgres_backup(target, maintenance_url=maintenance, **args)
        with engine.begin() as c:c.execute(text("UPDATE system_config SET value='\"after-backup\"' WHERE key='historical_marker'"))
        current = b.validate_postgres_restore(backup, target, maintenance, **args)
        assert 'source_schema_version' not in current and current['restore_validated']
        b.stage_postgres_restore(backup, target, maintenance, **args); engine.dispose()
        result = b.apply_pending_postgres_restore(target, maintenance, acknowledgement=backup_metadata['sha256'], **args)
        assert result['status'] == 'restored' and marker() == 'current-backup' and has_index(engine)
        with engine.connect() as c:validate_executables(c)
        print('post027-native-current-backup-restore-roundtrip-data-and-030-index-pass', flush=True)

        engine.dispose(); preserved029.integrity.check_package()
        with admin.connect() as c:
            assert c.execute(text("SELECT count(*) FROM pg_database WHERE datname LIKE 'odin_validate_%'")).scalar_one() == 0
            for name in ('post027_target', 'post027_fresh'):c.exec_driver_sql('DROP DATABASE IF EXISTS ' + name + ' WITH (FORCE)')
        admin.dispose()
    print('post027-native-pass', flush=True)


if __name__ == '__main__':
    main()
