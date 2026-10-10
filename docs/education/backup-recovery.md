# Education backup and recovery

The built-in backup is a **database backup**. A `.db` file is for SQLite; a `.dump` file is for PostgreSQL. It does not include uploaded print files, branding assets, installation identity, JWT signing material or printer encryption keys.

Keep a separate, protected backup or consistent snapshot of the full persistent data volume and any separately mounted upload volumes and secret files. Preserve the installation's existing keys. A database containing encrypted printer credentials cannot recover those credentials without the original encryption key. Do not send keys or full-volume archives in support email.

Before an upgrade, record the running image digest and make a matching backup of the database, files and installation material. Quiesce writes or stop the application when taking a full-volume filesystem snapshot so the database and files describe the same point in time. Protect the backup with restricted access.

In **Settings > System**, uploading a supported database backup validates and **stages** a restore. It does not replace the live database immediately. Restart during a maintenance window to apply the staged restore before normal startup. An automatic pre-restore database backup is created; it is not a substitute for the full-volume backup. If the response is lost, inspect server diagnostics and the backup list before retrying or restarting, because staging may already have succeeded.

After recovery, confirm application health, Education readiness, classroom membership and printer assignments, approval records, uploaded file availability and successful use of saved printer credentials. Health alone does not establish that the Education workflow is ready.

For rollback, restore the matching pre-upgrade data snapshot with the previous image. Do not run an older image against a newer database and assume compatibility. Keep the failed candidate data separately for investigation.

Local rehearsals use synthetic data. They do not establish that your backup is usable; rehearse recovery of your own installation into an isolated destination before relying on it.
