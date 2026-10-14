"""Backup acceptance must trust only packaged, literal-preserving trigger contracts."""
import pytest
from pathlib import Path
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db
from modules.system.backup_service import validate_database,BackupValidationError


@pytest.mark.parametrize('tamper',['altered_body','wrong_table','unknown_trigger','view','literal_case'])
def test_canonical_backup_rejects_executable_tampering(review_db,tamper):
    with review_db.get_bind().begin() as db:
        if tamper in ('altered_body','wrong_table'):
            name='cost_identities_no_update'
            canonical=db.execute(text("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=:name"),{'name':name}).scalar_one()
            db.execute(text('DROP TRIGGER '+name))
            if tamper=='wrong_table':definition=canonical.replace('ON cost_identities','ON users')
            else:definition=canonical.split('BEGIN',1)[0]+'BEGIN SELECT 1; END'
            db.exec_driver_sql(definition)
        elif tamper=='unknown_trigger':
            db.exec_driver_sql("CREATE TRIGGER synthetic_unknown BEFORE UPDATE ON users BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        elif tamper=='view':
            db.exec_driver_sql('CREATE VIEW synthetic_view AS SELECT id FROM users')
        else:
            db.exec_driver_sql('DROP TRIGGER trg_education_audit_no_update')
            db.exec_driver_sql("CREATE TRIGGER trg_education_audit_no_update BEFORE UPDATE ON education_audit_events BEGIN SELECT RAISE(ABORT,'EDUCATION AUDIT EVENTS ARE IMMUTABLE'); END")
    with pytest.raises(BackupValidationError,match='unexpected triggers or views'):
        validate_database(Path(review_db.get_bind().url.database))


def test_backup_trigger_contract_helper_pin_failure_is_closed(review_db,monkeypatch):
    import importlib
    contract=importlib.import_module('core.schema.migrations.009_cost_foundation')
    def unavailable():raise RuntimeError('synthetic packaged helper mismatch')
    monkeypatch.setattr(contract,'_helper_integrity',unavailable)
    with pytest.raises(BackupValidationError,match='Packaged backup trigger contract is unavailable'):
        validate_database(Path(review_db.get_bind().url.database))
