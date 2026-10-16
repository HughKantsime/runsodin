"""Staged historical restore under enterprise-compose secret ownership, as the entrypoint runs it.

Runs as root in the runtime image. Secrets are owned by uid 10001 (odin) with mode 0400,
as docker-compose.enterprise.yml mounts them. Prints post027-coordinator-* facts.
"""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import uuid

ODIN_UID = 10001


def owned(path: Path, mode: int):
    os.chown(path, ODIN_UID, ODIN_UID); path.chmod(mode)


def main():
    assert os.getuid() == 0
    raw = os.environ['POST027_NATIVE_URL']
    server = raw.replace('postgresql+psycopg://', 'postgresql://').rsplit('/', 1)[0]
    maintenance = server + '/postgres?sslmode=disable'
    target = server + '/coord_target'
    with tempfile.TemporaryDirectory(prefix='post027-coordinator-') as folder:
        root = Path(folder); root.chmod(0o755)
        secrets_dir = root / 'run-secrets'; secrets_dir.mkdir(mode=0o755)
        app_secret = secrets_dir / 'postgres_app_password'; app_secret.write_text(secrets.token_urlsafe(32)); owned(app_secret, 0o400)
        maintenance_secret = secrets_dir / 'postgres_maintenance_password'; maintenance_secret.write_text(secrets.token_urlsafe(32)); owned(maintenance_secret, 0o400)
        data = root / 'data'; (data / 'backups').mkdir(parents=True)
        for path in (data, data / 'backups'):
            os.chown(path, ODIN_UID, ODIN_UID)
        archives = root / 'archives'; archives.mkdir(); os.chown(archives, ODIN_UID, ODIN_UID)
        env = {**os.environ, 'DATABASE_URL': target, 'DATABASE_PASSWORD_FILE': str(app_secret), 'DATABASE_MAINTENANCE_URL': maintenance,
               'DATABASE_MAINTENANCE_PASSWORD_FILE': str(maintenance_secret), 'ODIN_DATA_DIR': str(data), 'JWT_SECRET_KEY': secrets.token_urlsafe(48)}
        os.environ.update(env)
        from sqlalchemy import create_engine, text
        admin = create_engine(raw, isolation_level='AUTOCOMMIT')
        with admin.connect() as c:
            c.exec_driver_sql('DROP DATABASE IF EXISTS coord_target WITH (FORCE)'); c.exec_driver_sql('CREATE DATABASE coord_target')

        def as_odin(code, extra=None):
            # Exactly how the entrypoint and the app run: runuser keeps the environment.
            return subprocess.run(['/usr/sbin/runuser', '-u', 'odin', '--', sys.executable, '-c', code], env={**env, **(extra or {})},
                                  capture_output=True, text=True, timeout=3600)

        boot = as_odin("from core.database_config import create_database_engine;from core.schema import bootstrap_database;from sqlalchemy import text\n"
                       "e=create_database_engine('" + target + "',role='bootstrap');bootstrap_database(e)\n"
                       "with e.begin() as c:c.execute(text(\"INSERT INTO system_config(key,value) VALUES('historical_marker','\\\"coord-target\\\"')\"))\n")
        assert boot.returncode == 0, boot.stderr[-3000:]
        from core.schema import preserved029
        bundle, _ = preserved029.integrity.check_package()
        made = subprocess.run(['/usr/sbin/runuser', '-u', 'odin', '--', sys.executable, str(Path(__file__).with_name('post027_archive_fixtures.py')),
                               raw, server, str(app_secret), str(archives)],
                              env={**env, 'PYTHONPATH': str(bundle), 'PYTHONDONTWRITEBYTECODE': '1'}, capture_output=True, text=True, timeout=1800)
        assert made.returncode == 0, made.stderr[-3000:]
        facts = json.loads(made.stdout.strip().splitlines()[-1])
        archive, digest = facts['25']['archive'], facts['25']['sha256']

        def marker():
            engine = create_engine(raw.rsplit('/', 1)[0] + '/coord_target')
            try:
                with engine.connect() as c:return c.execute(text("SELECT value FROM system_config WHERE key='historical_marker'")).scalar_one()
            finally:engine.dispose()

        stage = ("from pathlib import Path;from modules.system.postgres_backup_service import stage_postgres_restore;import os\n"
                 "stage_postgres_restore(Path('" + archive + "'),os.environ['DATABASE_URL'],os.environ['DATABASE_MAINTENANCE_URL'],"
                 "password_file=os.environ['DATABASE_PASSWORD_FILE'],maintenance_password_file=os.environ['DATABASE_MAINTENANCE_PASSWORD_FILE'])\n")
        staged = as_odin(stage)
        assert staged.returncode == 0, staged.stderr[-3000:]
        print('post027-coordinator-secrets-10001-0400-app-user-staged-real-025-archive-pass', flush=True)

        # Previous entrypoint: coordinator as root. The restore gate refuses odin-owned state:
        # an environment error, so the staged restore is kept and startup fails as before.
        old = subprocess.run([sys.executable, '-m', 'modules.system.restore_coordinator'], env={**env, 'ODIN_POSTGRES_RESTORE_ACKNOWLEDGEMENT': digest},
                             capture_output=True, text=True, timeout=3600)
        assert old.returncode != 0 and 'staged restore is kept' in old.stderr, old.stdout + old.stderr[-3000:]
        assert marker() == 'coord-target' and (data / 'restore-pending-postgres.json').exists() and (data / 'restore-pending.dump').exists()
        assert not list((data / 'backups').glob('failed_restore_*'))
        print('post027-coordinator-root-run-environment-error-keeps-staged-restore-and-fails-pass', flush=True)

        # Missing acknowledgement: an operator error, kept and failing, never quarantined.
        missing = subprocess.run(['/usr/sbin/runuser', '-u', 'odin', '--', sys.executable, '-m', 'modules.system.restore_coordinator'], env=env,
                                 capture_output=True, text=True, timeout=600)
        assert missing.returncode != 0 and 'RestoreAcknowledgementRequired' in missing.stderr, missing.stderr[-2000:]
        assert (data / 'restore-pending-postgres.json').exists() and not list((data / 'backups').glob('failed_restore_*'))
        print('post027-coordinator-missing-acknowledgement-keeps-staged-restore-pass', flush=True)

        # Deterministic archive refusal: the staged file no longer matches its manifest.
        good = (data / 'restore-pending.dump').read_bytes()
        pending = data / 'restore-pending.dump'
        pending.write_bytes(Path(facts['26']['archive']).read_bytes()); owned(pending, 0o600)
        refused = subprocess.run(['/usr/sbin/runuser', '-u', 'odin', '--', sys.executable, '-m', 'modules.system.restore_coordinator'],
                                 env={**env, 'ODIN_POSTGRES_RESTORE_ACKNOWLEDGEMENT': digest}, capture_output=True, text=True, timeout=3600)
        assert refused.returncode == 0, refused.stderr[-3000:]
        outcome = json.loads([line for line in refused.stdout.splitlines() if line.startswith('{"')][-1])
        assert outcome['status'] == 'restore_failed_target_untouched' and 'does not match its manifest' in outcome['reason'], outcome
        assert marker() == 'coord-target' and not (data / 'restore-pending-postgres.json').exists()
        listed = as_odin("import json;from modules.system.postgres_backup_service import quarantined_restores;print(json.dumps(quarantined_restores()))")
        assert listed.returncode == 0 and json.loads(listed.stdout.strip().splitlines()[-1])[0]['archive'], listed.stderr[-2000:]
        again = subprocess.run(['/usr/sbin/runuser', '-u', 'odin', '--', sys.executable, '-m', 'modules.system.restore_coordinator'], env=env,
                               capture_output=True, text=True, timeout=600)
        assert again.returncode == 0 and '"status"' not in again.stdout, again.stdout + again.stderr[-2000:]
        print('post027-coordinator-changed-staged-archive-quarantined-listed-next-boot-starts-on-untouched-target-pass', flush=True)
        del good

        # Current entrypoint: runuser -u odin. The staged historical restore applies.
        staged = as_odin(stage)
        assert staged.returncode == 0, staged.stderr[-3000:]
        entrypoint = Path('/source/docker/entrypoint.sh').read_text() if Path('/source/docker/entrypoint.sh').exists() else ''
        command = ['/usr/sbin/runuser', '-u', 'odin', '--', sys.executable, '-m', 'modules.system.restore_coordinator']
        applied = subprocess.run(command, env={**env, 'ODIN_POSTGRES_RESTORE_ACKNOWLEDGEMENT': digest}, capture_output=True, text=True, timeout=3600)
        assert applied.returncode == 0, applied.stderr[-3000:]
        assert '"status": "restored"' in applied.stdout, applied.stdout
        assert marker() == 'source25'
        with admin.connect() as c:
            assert c.execute(text("SELECT count(*) FROM pg_database WHERE datname LIKE 'odin_validate_%'")).scalar_one() == 0
            c.exec_driver_sql('DROP DATABASE IF EXISTS coord_target WITH (FORCE)')
        admin.dispose()
        print('post027-coordinator-runuser-odin-applied-staged-025-restore-pass', flush=True)
    print('post027-coordinator-pass', flush=True)


if __name__ == '__main__':
    main()
