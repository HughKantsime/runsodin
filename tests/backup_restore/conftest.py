import pytest


@pytest.fixture(autouse=True)
def owned_backup_alerts(monkeypatch):
    from modules.system import backup_failure_alerts
    original = backup_failure_alerts.publish_backup_failure
    monkeypatch.setattr(backup_failure_alerts, 'publish_backup_failure', lambda *args, **kwargs: None)
    return original


@pytest.fixture(autouse=True)
def owned_audit_evidence(tmp_path, monkeypatch):
    """Restore completion signs markers using only this test's private keyring."""
    monkeypatch.setenv('ODIN_AUDIT_KEY_DIR', str(tmp_path / 'audit-evidence'))
