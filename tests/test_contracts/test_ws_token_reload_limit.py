"""Reload budget on the real WS-token route, with in-process requests only."""
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded


def test_reload_budget_is_shared_by_accounts_on_the_same_ip(monkeypatch):
    from core import license_lock
    from core.db import get_db
    from core.dependencies import get_current_user
    from core.rate_limit import limiter
    from modules.organizations import routes_auth

    # Isolate issuance from authentication/storage: this test exercises the route's
    # actual limiter, not license policy, token contents, or a real user account.
    monkeypatch.setattr(license_lock, 'authorize_user', lambda *args, **kwargs: None)
    monkeypatch.setattr(routes_auth, 'capability_snapshot_id', lambda *args: 'test-snapshot')
    monkeypatch.setattr(routes_auth, 'create_access_token', lambda **kwargs: 'test-token')
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.include_router(routes_auth.router, prefix='/api')
    app.dependency_overrides[get_db] = lambda: None

    def principal(request: Request):
        return {'id': int(request.headers.get('x-test-user', '1')),
                'username': 'synthetic-viewer', 'role': 'viewer', 'group_id': 1}

    app.dependency_overrides[get_current_user] = principal

    @app.middleware('http')
    async def peer(request, call_next):
        request.scope['client'] = (request.headers.get('x-test-peer', '192.0.2.1'), 12345)
        return await call_next(request)

    limiter._storage.reset()
    try:
        with TestClient(app) as client:
            # Three ordinary reloads succeed on a fresh IP bucket.
            assert [client.post('/api/auth/ws-token').status_code for _ in range(3)] == [200] * 3
            # Another account/tab behind that IP spends the same remaining budget.
            assert [client.post('/api/auth/ws-token', headers={'x-test-user': '2'}).status_code
                    for _ in range(7)] == [200] * 7
            assert client.post('/api/auth/ws-token').status_code == 429
            assert client.post('/api/auth/ws-token', headers={'x-test-peer': '192.0.2.2'}).status_code == 200
    finally:
        limiter._storage.reset()
