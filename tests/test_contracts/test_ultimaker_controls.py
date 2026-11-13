"""Monitoring-only refusals at the real HTTP guard, before any device command."""
import pytest
from sqlalchemy import text

from modules.printers import support_levels
from tests.test_contracts.test_printer_support_levels import experimental_app, review_db, token_client  # noqa: F401


@pytest.mark.parametrize('action', ['pause', 'stop', 'resume'])
@pytest.mark.parametrize('api_type', ['ultimaker', ' Ultimaker '])
def test_monitor_only_controls_return_a_permanent_refusal(experimental_app, review_db, monkeypatch, action, api_type):
    from modules.printers import routes_controls, route_utils
    client, headers, sent = experimental_app
    review_db.execute(text("UPDATE printers SET api_type=:kind,gcode_state='PRINTING' WHERE id=9"), {'kind': api_type})
    review_db.commit()
    # Use the real dispatcher to reproduce the old 503 without any physical I/O.
    monkeypatch.setattr(routes_controls, '_send_printer_command', route_utils._send_printer_command)
    response = client.post(f'/printers/9/{action}', headers=headers)
    assert response.status_code == 409, response.text
    assert response.json()['detail'] == {
        'code': support_levels.MONITOR_ONLY_NOT_AVAILABLE,
        'message': support_levels.MONITOR_ONLY_DETAIL,
    }
    assert 'Retry-After' not in response.headers
    assert sent == []
    review_db.expire_all()
    assert review_db.execute(text('SELECT gcode_state FROM printers WHERE id=9')).scalar_one() == 'PRINTING'
    assert review_db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='printer.experimental_control_refused' AND entity_id=9")).scalar_one() == 1
