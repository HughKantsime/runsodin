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
        from core.timelapse_capture_tenancy_schema import TABLE as TENANCY, validate as validate_033
        import importlib
        _m034 = importlib.import_module('core.schema.migrations.034_alert_outbox')
        OUTBOX, validate_034 = _m034.TABLE, _m034.validate

        def fresh(name):
            with admin.connect() as c:
                c.exec_driver_sql('DROP DATABASE IF EXISTS ' + name + ' WITH (FORCE)'); c.exec_driver_sql('CREATE DATABASE ' + name)
            return create_database_engine(server + '/' + name, role='restore', password_file=str(credential))

        def has_index(engine):
            # Migration 032's table, exactly shaped (name kept from the 030 proof's helper).
            with engine.connect() as c:
                validate_032(c)
                validate_033(c)
                validate_034(c)
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

        if os.environ.get('POST027_CURRENT_ONLY') != '1':
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

        else:
            # A scoped rerun after the historical phases have already passed.
            # Keep skipped phases explicit; do not print their pass markers.
            with engine.begin() as c:
                c.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(80,'Owned','Era fixture','PLA'),(81,'Owned','Second','PETG')"))
            print('post027-native-current-only-historical-phases-not-repeated',flush=True)

        # Released034 archive must pass its own frozen validation, then live035 upgrade.
        from core.schema import preserved034
        frozen034, _ = preserved034.check_package()
        old034 = fresh('post027_published034'); old034.dispose()
        env034 = {**os.environ, 'DATABASE_URL':server+'/post027_published034', 'PYTHONPATH':str(frozen034), 'PYTHONDONTWRITEBYTECODE':'1'}
        make034 = subprocess.run([sys.executable,'-c',
            "from core.database_config import create_database_engine; from core.schema import bootstrap_database; import os; "
            "e=create_database_engine(os.environ['DATABASE_URL'],role='restore',password_file=os.environ['DATABASE_PASSWORD_FILE']); bootstrap_database(e); e.dispose()"],
            cwd=root,env=env034,capture_output=True,text=True,timeout=300)
        assert make034.returncode==0, make034.stderr
        archive034=root/'published034.dump'
        b._run_pg_tool(['pg_dump','--format=custom','--schema','public','--no-owner','--no-privileges','--no-comments','--file',str(archive034),'--dbname',server+'/post027_published034'],maintenance,str(credential),root)
        facts034=b.validate_postgres_restore(archive034,target,maintenance,**args)
        assert facts034['source_schema_version']==34 and facts034['prepared_schema_version']==35
        assert leftover()==0
        print('post027-native-published034-frozen-stage-live035-upgrade-validated-pass',flush=True)

        # 5. Current-schema backup -> restore round trip keeps data, including a 032 row.
        with engine.begin() as c:
            c.execute(text("UPDATE system_config SET value='\"current-backup\"' WHERE key='historical_marker'"))
            c.execute(text("INSERT INTO " + DEFERRED + "(id,user_id,org_id,scope_key,channel,alert_type,severity,title,state) "
                           "VALUES('roundtrip032',3,1,'org:1','email','print_failed','critical','kept','pending')"))
            # Migration 033: a capture-tenancy row (with its ownership anchor) survives too.
            c.execute(text("INSERT INTO timelapse_ownership_anchors(operation_id,timelapse_id,printer_id,filename,owner_state,"
                           "ownership_source,capture_printer_shared) VALUES('roundtrip033',1,1,'roundtrip.mp4','unresolved','capture_unresolved',FALSE)"))
            c.execute(text("INSERT INTO " + TENANCY + "(operation_id,captured_single_tenant) VALUES('roundtrip033',TRUE)"))
            # Migration 034: an undispatched alert outbox row (with its job) survives too.
            c.execute(text("INSERT INTO jobs(id,item_name,status) VALUES(990034,'roundtrip034','pending')"))
            c.execute(text("INSERT INTO " + OUTBOX + "(job_id,alert_kind) VALUES(990034,'queue_added')"))
            #035 immutable recipe, inspection and pending request all survive restore.
            c.execute(text("INSERT INTO production_recipes(id,owner,definition) VALUES(:id,1,'{}')"),{'id':'a'*32})
            c.execute(text("INSERT INTO production_acceptances(id,owner,job_uid,claim_token,recipe_id,request_hash,definition) VALUES(:id,1,:uid,:claim,:recipe,:hash,'{}')"),{'id':'b'*32,'uid':'u'*36,'claim':'c'*32,'recipe':'a'*32,'hash':'d'*64})
            c.execute(text("INSERT INTO production_stock_additions(id,owner,receipt_id,destination,request_body,state,attempts) VALUES(:id,1,:receipt,'{}','{}','prepared',0)"),{'id':'e'*32,'receipt':'b'*32})
            #040 Foundation rows survive the same production backup/restore path.
            c.execute(text("INSERT INTO job_summary_outbox(print_attempt_uid,attempt_birth,job_id,outcome) VALUES('roundtrip040-attempt','roundtrip040-birth',990034,'completed')"))
            c.execute(text("INSERT INTO stock_snapshots(owner_scope,owner_key,variation,location,quantity,calculated_at,read_at) VALUES('global',0,'roundtrip040','main','3',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
            c.execute(text("INSERT INTO job_timeline_events(id,job_id,print_attempt_uid,kind,occurred_at) VALUES('roundtrip040',990034,'roundtrip040-attempt','completed',CURRENT_TIMESTAMP)"))
            c.execute(text("INSERT INTO timeline_writer_state(process_id,started_at,clean_shutdown,last_drained_at) VALUES('roundtrip040',CURRENT_TIMESTAMP,1,CURRENT_TIMESTAMP)"))
            c.execute(text("INSERT INTO dispatch_attempt_receipts(claim_token,printer_id,job_id,configuration_key,prepared_power_key,observed_filename_key,stable_configuration_key,stable_key_source) VALUES(:token,1,990034,:full,:full,:full,:stable,'dispatch')"), {'token':'4'*32,'full':'4'*64,'stable':'1'*64})
        backup, backup_metadata = b.create_postgres_backup(target, maintenance_url=maintenance, **args)
        with engine.begin() as c:
            c.execute(text("UPDATE system_config SET value='\"after-backup\"' WHERE key='historical_marker'"))
            c.execute(text("DELETE FROM " + DEFERRED))
            c.execute(text("DELETE FROM " + TENANCY))
            c.execute(text("DELETE FROM " + OUTBOX))
            for table035 in ('production_stock_additions','production_acceptances','production_recipes'):
                c.execute(text('DELETE FROM '+table035))
        current = b.validate_postgres_restore(backup, target, maintenance, **args)
        assert 'source_schema_version' not in current and current['restore_validated']
        b.stage_postgres_restore(backup, target, maintenance, **args); engine.dispose()
        result = b.apply_pending_postgres_restore(target, maintenance, acknowledgement=backup_metadata['sha256'], **args)
        assert result['status'] == 'restored' and marker() == 'current-backup' and has_index(engine)
        with engine.connect() as c:
            validate_executables(c)
            assert c.execute(text("SELECT id,title FROM " + DEFERRED)).all() == [('roundtrip032', 'kept')]
            assert c.execute(text("SELECT operation_id,captured_single_tenant FROM " + TENANCY)).all() == [('roundtrip033', True)]
            assert c.execute(text("SELECT job_id,alert_kind,attempts FROM " + OUTBOX)).all() == [(990034, 'queue_added', 0)]
        print('post027-native-current-backup-restore-roundtrip-data-and-032-row-pass', flush=True)
        print('post027-native-current-backup-restore-roundtrip-033-capture-tenancy-row-pass', flush=True)
        with engine.connect() as c:
            for table035 in ('production_recipes','production_acceptances','production_stock_additions'):
                assert c.execute(text('SELECT COUNT(*) FROM '+table035)).scalar_one()==1
        print('post027-native-current-backup-restore-roundtrip-034-alert-outbox-row-pass', flush=True)
        print('post027-native-current-backup-restore-roundtrip-035-all-three-rows-pass',flush=True)
        with engine.connect() as c:
            assert c.execute(text("SELECT outcome FROM job_summary_outbox WHERE print_attempt_uid='roundtrip040-attempt'")).scalar_one()=='completed'
            assert c.execute(text("SELECT quantity FROM stock_snapshots WHERE variation='roundtrip040'")).scalar_one()=='3'
            assert c.execute(text("SELECT kind FROM job_timeline_events WHERE id='roundtrip040'")).scalar_one()=='completed'
            assert c.execute(text("SELECT clean_shutdown FROM timeline_writer_state WHERE process_id='roundtrip040'")).scalar_one()==1
        print('post027-native-current-backup-restore-roundtrip-040-all-four-rows-pass',flush=True)
        with engine.connect() as c:
            assert c.execute(text('SELECT stable_configuration_key,stable_key_source FROM dispatch_attempt_receipts WHERE claim_token=:token'), {'token':'4'*32}).one()==('1'*64,'dispatch')
        print('post027-native-current-backup-restore-roundtrip-043-recipe-key-pass',flush=True)


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
        before = marker(); engine.dispose()  # no open target session during the apply
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
