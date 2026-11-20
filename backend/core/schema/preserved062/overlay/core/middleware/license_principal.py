"""Fleet-lock principal gate with a direct ASGI path when disabled."""
from fastapi import HTTPException
from starlette.requests import Request


class LicensePrincipalMiddleware:
    def __init__(self, app, is_public_browser_request):
        self.app = app
        self.is_public_browser_request = is_public_browser_request

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        from core.license_lock import enabled
        if not enabled():
            return await self.app(scope, receive, send)
        request = Request(scope)
        path = request.scope.get('path', '')
        api_path = path[7:] if path.startswith('/api/v1/') else path[4:] if path.startswith('/api/') else path
        public = (request.method == 'OPTIONS' or self.is_public_browser_request(request)
                  or api_path == '/license' and request.method in {'GET', 'HEAD'}
                  or api_path in {'/health', '/setup'}
                  or api_path.startswith(('/auth/', '/health/', '/setup/', '/static/'))
                  or path.startswith(('/static/', '/assets/'))
                  or api_path == '/branding' and request.method == 'GET')
        # Defer only when credentials are exclusively route-specific carriers.
        # An added query token/API key must never bypass a session/bearer lock.
        authorization = request.headers.get('authorization', '')
        scheme, _, bearer = authorization.partition(' ')
        understood = (request.cookies.get('session') or
                      scheme.lower() == 'bearer' and bearer.strip())
        if understood and not public:
            from core.dependencies import resolve_current_user_sync
            from core.db import SessionLocal
            from starlette.concurrency import run_in_threadpool
            authorization = request.headers.get('authorization', '')
            scheme, _, bearer = authorization.partition(' ')
            def resolve():
                with SessionLocal() as policy_db:
                    return resolve_current_user_sync(request, bearer if scheme.lower() == 'bearer' else None,
                                                     policy_db, side_effects=False)
            try:
                request.state.resolved_license_principal = await run_in_threadpool(resolve)
            except HTTPException as exc:
                from fastapi.responses import JSONResponse
                return await JSONResponse(status_code=exc.status_code, content={'detail': exc.detail}, headers=exc.headers)(scope, receive, send)
            except Exception:
                from fastapi.responses import JSONResponse
                return await JSONResponse(status_code=503, content={'detail': 'Service temporarily unavailable'})(scope, receive, send)
        return await self.app(scope, receive, send)
