"""Exercise printer operations through their HTTP routes with synthetic devices."""
import pytest
from sqlalchemy import text

from tests.test_contracts.test_cert_A_harness import cert_app, cert_client, fernet_env, review_db


@pytest.mark.parametrize('role,group_id', [('operator', 1), ('operator', None), ('admin', None)])
@pytest.mark.parametrize('printer_org', [None, 2])
def test_operating_printer_scope(cert_app, review_db, monkeypatch, role, group_id, printer_org):
    from modules.printers import monitor_commands, print_objects, routes_controls, routes_start_next, smart_plug

    review_db.execute(text("UPDATE printers SET org_id=:org,plug_type='mqtt' WHERE id=301"), {'org': printer_org})
    review_db.execute(text("UPDATE users SET group_id=:org WHERE username=:name"), {'org': group_id, 'name': role})
    review_db.commit()
    calls = []

    def send(*args, **kwargs):
        calls.append(args)
        return True

    def accept_monitor_command(*args, **kwargs):
        calls.append(args)
        return 'accepted'

    monkeypatch.setattr(routes_controls, '_send_printer_command', send)
    monkeypatch.setattr(routes_controls, '_bambu_command_direct', send)
    monkeypatch.setattr(monitor_commands, 'request_command', accept_monitor_command)
    monkeypatch.setattr(print_objects, 'running_print_objects', lambda *a: dict(
        supported=True, print=dict(print_job_id=11, job_id=12), source='bambu_3mf',
        objects=[dict(id=str(n), state='printing') for n in (1, 2)]))
    for action in ('power_on', 'power_off', 'power_toggle'):
        monkeypatch.setattr(smart_plug, action, send)
    # Stop at the dispatch boundary. The actual router still loads and authorizes the printer.
    def start(db, printer, user, data, **kwargs):
        calls.append((printer.id,))
        return dict(success=True, state='preparing', job_id=data.suggested_job_id, request_id=str(data.request_id))
    monkeypatch.setattr(routes_start_next, 'start', start)
    cert_app.app.include_router(routes_start_next.router)
    cases = [
        ('stop', None), ('pause', None), ('resume', None), ('clear-errors', None),
        ('skip-objects', {'print_job_id': 11, 'object_ids': [1]}), ('speed', {'speed': 2}),
        ('fan', {'fan': 'chamber', 'speed': 10}), ('lights', None), ('plate-cleared', None),
        ('plug/on', None), ('plug/off', None), ('plug/toggle', None),
        ('start-next', dict(suggested_job_id=11, suggestion_fingerprint='a' * 64,
                            request_id='7c31107a-4b05-4b92-a12a-ba69885947d1', bed_clear_confirmed=True)),
    ]
    foreign = printer_org is not None and role != 'admin'
    for path, body in cases:
        response = cert_app.post(f'/printers/301/{path}', headers=cert_app.roles[role], json=body)
        success = 202 if path == 'start-next' else 200
        assert response.status_code == (404 if foreign else success), (path, response.text)
    if foreign:
        assert calls == []
