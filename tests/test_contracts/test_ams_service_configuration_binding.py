"""Configuration is captured under the same lock as its generation."""
from sqlalchemy import text
from tests.test_contracts.test_ams_evidence_transactions import db,RUNTIME

def test_config_change_at_begin_never_captures_old_transport(db,monkeypatch):
    from modules.printers import services
    from modules.printers.telemetry.bambu import ams_evidence_transactions as transactions
    import core.runtime_identity as identity
    monkeypatch.setattr(identity,'read_runtime_identity',lambda:RUNTIME)
    original=transactions._begin_read
    def changed_before_lock(*args,**kwargs):
        db.execute(text("UPDATE printers SET api_host='changed.local'"))
        db.commit()
        return original(*args,**kwargs)
    monkeypatch.setattr(transactions,'_begin_read',changed_before_lock)
    operation=services.begin_ams_read(db,printer_id=1,expected_org_id=2)
    assert operation._configuration.transport.host=='changed.local'
    assert operation._configuration.configuration_key==operation._started.configuration_key
    db.rollback()
