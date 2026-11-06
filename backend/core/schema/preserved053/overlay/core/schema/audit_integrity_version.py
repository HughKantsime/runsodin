"""Change this identity (and rename its migration file) when merging tracks."""
MIGRATION_ID = 'python:047-audit-integrity'
NUMBER = MIGRATION_ID.split(':')[1].split('-')[0]
MODULE = f'core.schema.migrations.{NUMBER}_audit_integrity'
