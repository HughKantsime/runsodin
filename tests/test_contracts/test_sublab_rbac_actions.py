"""Real HTTP admission checks without issuing physical or provider writes.

Empty bodies intentionally stop at request validation after dependencies pass;
send stops at the disabled two-key gate before any provider write.
"""
import pytest
from core.rbac_policy import RBAC_DEFAULT_ACTION_ACCESS
from tests.test_contracts.sublab_rbac_cases import CASES
from tests.test_contracts.test_cert_G_harness import run_probe

PROBE = r'''
from tests.test_contracts.sublab_rbac_cases import CASES
install_license('enterprise')
sql("INSERT INTO groups(id,name,is_org) VALUES (1,'Sublab matrix org',1)")
super_id = seed_user('sublab-super', 'admin')
admin_id = seed_user('sublab-admin', 'admin', 1)
operator_id = seed_user('sublab-operator', 'operator', 1)
viewer_id = seed_user('sublab-viewer', 'viewer', 1)
clients = {role: login('sublab-' + role) for role in ('super', 'admin', 'operator', 'viewer')}
super_client = clients['super']
defaults = super_client.get('/api/permissions').json()
def roles(key, allowed):
    import copy
    doc = copy.deepcopy(defaults)
    doc['action_access'][key] = allowed
    response = super_client.put('/api/permissions', json=doc)
    assert response.status_code == 200, response.text

def call(role, method, path):
    response = clients[role].request(method, path, json={})
    return {'status': response.status_code, 'body': response.json()}
OUT['actions'] = {}
for key, (method, path, expected) in CASES.items():
    allowed = defaults['action_access'][key]
    holder = 'operator' if 'operator' in allowed and key != 'products.recipes' else 'admin'
    row = OUT['actions'][key] = {'holder': call(holder, method, path)}
    row['viewer'] = call('viewer', method, path)
    row['operator'] = call('operator', method, path)
    roles(key, [])
    row['narrowed'] = call(holder, method, path)
    row['superadmin'] = call('super', method, path)
    roles(key, ['admin', 'operator', 'viewer'])
    row['widened_viewer'] = call('viewer', method, path)
    if holder == 'admin':
        row['widened_operator'] = call('operator', method, path)
    assert super_client.post('/api/permissions/reset').status_code == 200
'''

@pytest.fixture(scope='module')
def admission(tmp_path_factory):
    return run_probe('sublab-rbac', PROBE, tmp_path_factory.mktemp('sublab-rbac'))['actions']

@pytest.mark.parametrize('key', sorted(CASES))
def test_sublab_matrix_and_independent_role_floor(admission, key):
    row = admission[key]
    expected = CASES[key][2]
    assert row['holder']['status'] == expected, row
    assert row['superadmin']['status'] == expected, row
    assert row['viewer']['status'] == 403, row
    assert row['narrowed']['status'] == 403, row
    assert row['narrowed']['body']['detail'] == 'Insufficient permissions', row
    if key == 'products.square_send':
        assert row['holder']['body']['detail'] == 'Square stock writes require both activation keys', row
    assert row['widened_viewer']['status'] == 403, row
    if 'operator' not in RBAC_DEFAULT_ACTION_ACCESS[key] or key == 'products.recipes':
        assert row['operator']['status'] == 403, row
        assert row['widened_operator']['status'] == 403, row
    else:
        assert row['operator']['status'] == expected, row
