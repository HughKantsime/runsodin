"""Every production fetch/fetchAPI route and method exists on the registered app.

Uses TypeScript's AST (frontend npm dependencies required) and the real backend
route table in an owned SQLite subprocess. No Docker or listening socket needed.
"""
import json
import re
import subprocess
from pathlib import Path

import pytest

from tests.test_contracts.test_cert_G_harness import run_probe

ROOT = Path(__file__).resolve().parents[2]
EXTRACTOR = ROOT / 'frontend/scripts/api-route-calls.mjs'


def extract(source=None):
    command = ['node', str(EXTRACTOR)] + (['--stdin'] if source is not None else [])
    result = subprocess.run(command, input=source, text=True, capture_output=True, check=True, cwd=ROOT)
    return json.loads(result.stdout)


@pytest.fixture(scope='module')
def routes(tmp_path_factory):
    result = run_probe('frontend-routes', '''
from fastapi.routing import APIRoute
OUT['routes'] = [(method, r.path) for r in app.routes if isinstance(r, APIRoute) for method in r.methods]
''', tmp_path_factory.mktemp('frontend-routes'))
    assert result['_net_attempts'] == []
    return result['routes']


def canonical(path):
    return re.sub(r'\{[^}]+\}', '{param}', path.split('?')[0].split('#')[0])


def missing(calls, routes, *, allow_dynamic=True):
    known = {(method, canonical(path)) for method, path in routes}
    patterns = [(method, re.compile('^' + re.escape(canonical(path)).replace(r'\{param\}', '[^/]+') + '$'))
                for method, path in routes]
    allowlist = json.loads((Path(__file__).with_name('frontend_api_dynamic_allowlist.json')).read_text()) if allow_dynamic else []
    exemptions = {(item['file'], item['expression'], item['options']): item for item in allowlist}
    errors, used = [], set()
    for call in calls:
        key = (call['file'], call['expression'], call['options'])
        if key in exemptions:
            item = exemptions[key]
            assert item['reason'].strip()
            used.add(key)
            pairs = item['routes']
        else:
            pairs = [(method, path) for method in call['methods'] for path in call['paths']]
            assert pairs, call  # unsupported AST must never silently disappear
        for method, path in pairs:
            path = canonical(path)
            if (method, path) not in known and not any(m == method and pattern.fullmatch(path) for m, pattern in patterns):
                errors.append(f"{call['file']}:{call['line']}: {method} {path}")
    if allow_dynamic:
        errors.extend(f'Stale dynamic allowlist entry: {key[:2]}' for key in exemptions.keys() - used)
    return errors


def test_frontend_api_routes_exist(routes):
    calls = extract()
    failures = missing(calls, routes)
    assert len(calls) > 500
    assert not failures, '\n'.join(failures)


def test_old_setup_offline_path_is_rejected(routes):
    source = (ROOT / 'frontend/src/pages/auth/Setup.tsx').read_text()
    assert "fetch('/api/license/upload'," in source
    old = source.replace("fetch('/api/license/upload',", "fetch('/api/license/activate/offline',")
    failures = missing(extract(old), routes, allow_dynamic=False)
    assert any('POST /api/license/activate/offline' in failure for failure in failures), failures


@pytest.mark.parametrize('source,expected', [
    ("fetch('/api/health')", [('GET', '/api/health')]),
    ("fetchAPI(`/cameras/${cameraId}/webrtc`, {method: 'POST'})", [('POST', '/api/cameras/{param}/webrtc')]),
    ("fetchAPI('/jobs/' + id, {method: 'PATCH', body: JSON.stringify({method: 'DELETE'})})", [('PATCH', '/api/jobs/{param}')]),
    ("fetch(`/api/items/${id}?q=${query}`)", [('GET', '/api/items/{param}')]),
    ("// fetch('/api/not-a-call')\nfetchAPI('/health')", [('GET', '/api/health')]),
    ("const base='/api'; fetch(`${base}/license/upload`, {method:'POST'})", [('POST', '/api/license/upload')]),
    ("const action=(kind:'trim'|'speed')=>fetch(`/api/items/${id}/${kind}`, {method:'POST'})", [('POST', '/api/items/{param}/trim'), ('POST', '/api/items/{param}/speed')]),
])
def test_parser_handles_urls_and_methods(source, expected):
    calls = extract(source)
    pairs = [(method, canonical(path)) for call in calls for method in call['methods'] for path in call['paths']]
    assert pairs == expected


def test_contract_refuses_wrong_method_and_unreviewed_dynamic_call():
    assert missing(extract("fetch('/api/health', {method:'DELETE'})"), [('GET', '/api/health')], allow_dynamic=False)
    assert missing(extract('fetch(url, options)'), [('GET', '/api/health')], allow_dynamic=False)
