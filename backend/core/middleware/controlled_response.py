"""Apply aggregate-name redaction without buffering unrestricted responses."""
import json
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse


async def _response_actor(request):
    # Idempotency can return without invoking route dependencies. Resolve the
    # replaying principal through the same authentication boundary in that case.
    if not hasattr(request.state, 'controlled_actor'):
        from core.db import SessionLocal
        from core.dependencies import _resolve_current_user
        from core.controlled_parts import any_read_restrictions
        authorization = request.headers.get('authorization', '')
        token = authorization[7:] if authorization.lower().startswith('bearer ') else None
        with SessionLocal() as db:
            db.info['_controlled_request'] = True
            request.state.controlled_actor = await _resolve_current_user(request, token, db)
            request.state.controlled_marks = bool(request.state.controlled_actor) and any_read_restrictions(db)
    return getattr(request.state, 'controlled_actor', None) if getattr(request.state, 'controlled_marks', False) else None


def _redact(request, actor, content):
    from core.db import SessionLocal
    from core.controlled_redaction import COLLECTIONS, redact_export
    with SessionLocal() as db:
        db.info.update(_controlled_request=True, _controlled_any=True)
        parts = request.url.path.strip('/').split('/')
        kind = next((COLLECTIONS[p] for p in parts if p in COLLECTIONS), None)
        value = redact_export(db, actor, json.loads(content), resource_kind=kind)
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()


class ControlledResponseMiddleware:
    """Only restricted JSON needs a body copy; all other ASGI messages pass through."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        request = Request(scope)
        start = None
        actor = None
        refused = False
        chunks = []

        async def wrapped_send(message):
            nonlocal start, actor, refused
            if message['type'] == 'http.response.start':
                status = message['status']
                headers = Headers(raw=message.get('headers', []))
                if (200 <= status < 300 and status != 204 and request.method != 'HEAD'
                        and headers.get('content-type', '').split(';')[0] == 'application/json'):
                    try:
                        actor = await _response_actor(request)
                    except HTTPException as exc:
                        # User middleware is outside ExceptionMiddleware. Send
                        # auth refusals before any cached response bytes escape.
                        refused = True
                        response = JSONResponse({'detail': exc.detail}, status_code=exc.status_code, headers=exc.headers)
                        await response(scope, receive, send)
                        return
                    if actor:
                        start = message
                        return
            if refused:
                return
            if start is not None and message['type'] == 'http.response.body':
                chunks.append(message.get('body', b''))
                if message.get('more_body', False):
                    return
                content = b''.join(chunks)
                body = await run_in_threadpool(_redact, request, actor, content) if content else content
                # Retain duplicate headers (especially Set-Cookie).
                headers = [(k, v) for k, v in start.get('headers', []) if k.lower() != b'content-length']
                await send({**start, 'headers': headers + [(b'content-length', str(len(body)).encode())]})
                await send({**message, 'body': body})
                return
            await send(message)

        await self.app(scope, receive, wrapped_send)
