"""Configuration, reads, and legacy group compatibility through the real routes."""
import pytest
from sqlalchemy import text

from tests.test_contracts.test_education_classes_migration import pg_engine
from tests.test_contracts.test_cert_A_harness import cert_app, cert_client, fernet_env, review_db


@pytest.mark.parametrize('role,group,printer_org,has_orgs', [
    ('admin', 1, None, True), ('admin', 1, 2, True),
    ('admin', None, None, True), ('admin', None, 2, True),
    ('operator', None, None, True), ('operator', None, 2, True),
    ('operator', None, None, False), ('admin', None, None, False),
    ('operator', 1, None, False), ('admin', 1, None, False),
])
def test_configuration_scope(cert_app, review_db, monkeypatch, role, group, printer_org, has_orgs):
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.printers import camera_routes, routes_maintenance_state
    from modules.vision.routes import detections

    for module in (camera_routes, routes_maintenance_state, detections):
        cert_app.app.include_router(module.router)
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    if not has_orgs:
        review_db.execute(text('UPDATE groups SET is_org=FALSE'))
    review_db.execute(text('UPDATE users SET group_id=:org WHERE username=:role'), {'role': role, 'org': group})
    review_db.execute(text('UPDATE printers SET org_id=:org WHERE id=301'), {'org': printer_org})
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(301,1,'PLA')"))
    review_db.execute(text("INSERT INTO nozzle_lifecycle(id,printer_id,nozzle_type) VALUES(500,301,'brass')"))
    review_db.commit()
    printer = '/printers/301'
    headers = cert_app.roles[role]
    foreign = printer_org == 2 and (group is not None or role != 'admin')
    for suffix in ('', '/slots', '/plug', '/maintenance-state', '/nozzle', '/nozzle/history', '/vision'):
        response = cert_app.get(printer + suffix, headers=headers)
        assert response.status_code == (404 if foreign else 200), (suffix, response.text)

    cases = [
        ('patch', printer, {'name': 'Changed'}, False),
        ('post', '/printers/reorder', {'printer_ids': [301]}, False),
        ('patch', printer + '/slots/1', {}, False),
        ('put', printer + '/plug', {'auto_on': False}, False),
        ('delete', printer + '/plug', None, False),
        ('put', printer + '/maintenance-state', {'state': 'warn', 'note': 'Inspection'}, True),
        ('patch', printer + '/nozzle/500/retire', None, False),
        ('post', printer + '/nozzle', {'nozzle_type': 'brass'}, False),
        ('patch', '/cameras/301/toggle', None, False),
        ('patch', printer + '/vision', {'enabled': False}, True),
        ('post', '/printers/bulk-update', {'printer_ids': [301], 'action': 'disable'}, True),
        ('delete', printer, None, True),
    ]
    for method, path, body, admin_only in cases:
        response = cert_app.request(method, path, json=body, headers=headers)
        refused = has_orgs and group == 1 and printer_org is None
        if admin_only and role != 'admin':
            assert response.status_code == 403, path
        elif foreign:
            assert response.status_code == 404, path
        elif refused and path.endswith('/bulk-update'):
            assert response.status_code == 200 and response.json()['affected'] == 0
            assert [entry['printer_id'] for entry in response.json()['skipped']] == [301]
        elif refused and '/slots/' in path:
            # Loading filament is operating the printer: members keep it.
            assert 200 <= response.status_code < 300, (path, response.text)
        elif refused:
            assert response.status_code == 403, path
            assert response.json()['detail'] == 'This printer belongs to no organization. Ask a platform administrator to change it.'
        else:
            assert 200 <= response.status_code < 300, (path, response.text)


def test_write_predicate_requires_user_and_preserves_scoped_access():
    from core.rbac import check_org_write
    member = {'role': 'operator', 'group_id': 1}
    for operate in (True, False):
        assert check_org_write(member, None, operate=operate, db=object()) is operate
        assert not check_org_write(None, None, operate=operate)
        assert not check_org_write({}, 1, operate=operate)
        assert check_org_write(member, 1, operate=operate)
        assert not check_org_write(member, 2, operate=operate)


@pytest.mark.postgresql
def test_native_group_membership_is_resolved_by_authentication(pg_engine):
    from sqlalchemy.orm import Session
    from core.auth import create_access_token
    from core.dependencies import validate_access_token
    from core.rbac import check_org_write
    from core.schema import bootstrap_database

    bootstrap_database(pg_engine)
    with Session(pg_engine) as db:
        db.execute(text("INSERT INTO groups(id,name,is_org) VALUES(711,'Ordinary group',FALSE)"))
        db.execute(text("INSERT INTO users(id,username,password_hash,role,group_id,is_active) "
                        "VALUES(711,'group-operator','fixture','operator',711,TRUE)"))
        token = create_access_token({'sub': 'group-operator', 'user_id': 711, 'role': 'operator'})
        assert check_org_write(validate_access_token(token, db), None)
        db.execute(text('UPDATE groups SET is_org=TRUE WHERE id=711'))
        assert not check_org_write(validate_access_token(token, db), None)
