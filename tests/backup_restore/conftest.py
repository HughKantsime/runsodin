import pytest


@pytest.fixture(autouse=True)
def owned_backup_alerts(monkeypatch):
    from modules.system import backup_failure_alerts
    original = backup_failure_alerts.publish_backup_failure
    monkeypatch.setattr(backup_failure_alerts, 'publish_backup_failure', lambda *args, **kwargs: None)
    return original
