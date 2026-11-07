"""Apply aggregate-name redaction to authenticated JSON, including cached replies."""
import json
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse


async def controlled_response(request, call_next):
    response = await call_next(request)
    if (not 200 <= response.status_code < 300 or response.status_code == 204
            or request.method == 'HEAD'
            or response.headers.get('content-type', '').split(';')[0] != 'application/json'):
        return response
    # Idempotency can return without invoking route dependencies. Resolve the
    # replaying principal through the same authentication boundary in that case.
    if not hasattr(request.state, 'controlled_actor'):
        from core.db import SessionLocal
        from core.dependencies import _resolve_current_user
        from core.controlled_parts import any_read_restrictions
        authorization = request.headers.get('authorization', '')
        token = authorization[7:] if authorization.lower().startswith('bearer ') else None
        with SessionLocal() as db:
            try:
                request.state.controlled_actor = await _resolve_current_user(request, token, db)
            except HTTPException as exc:
                # User middleware sits outside ExceptionMiddleware. Preserve an
                # authentication refusal here rather than turning it into a 500.
                return JSONResponse({'detail': exc.detail}, status_code=exc.status_code, headers=exc.headers)
            request.state.controlled_marks = bool(request.state.controlled_actor) and any_read_restrictions(db)
    actor = getattr(request.state, 'controlled_actor', None)
    if not actor or not getattr(request.state, 'controlled_marks', False):
        return response
    if response.headers.get('content-type', '').split(';')[0] != 'application/json':
        return response
    content = b''.join([chunk async for chunk in response.body_iterator])
    if not content or request.method == 'HEAD':
        return Response(content, status_code=response.status_code, headers=dict(response.headers), background=response.background)
    def redact():
        from core.db import SessionLocal
        from core.controlled_redaction import redact_export
        with SessionLocal() as db:
            db.info.update(_controlled_request=True, _controlled_any=True)
            from core.controlled_redaction import COLLECTIONS
            parts = request.url.path.strip('/').split('/')
            kind = next((COLLECTIONS[p] for p in parts if p in COLLECTIONS), None)
            return redact_export(db, actor, json.loads(content), resource_kind=kind)
    body = json.dumps(await run_in_threadpool(redact), ensure_ascii=False, separators=(',', ':')).encode()
    headers = dict(response.headers)
    headers['content-length'] = str(len(body))
    result = Response(body, status_code=response.status_code, headers=headers, background=response.background)
    # Retain duplicate Set-Cookie headers.
    result.raw_headers = [(k, v) for k, v in response.raw_headers if k.lower() != b'content-length'] + [(b'content-length', str(len(body)).encode())]
    return result
