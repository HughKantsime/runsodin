"""Pure ASGI final-send clearing, before Starlette response background tasks."""
from core.audit_actor import RequestAuditScope, _SCOPE_KEY

class AuditActorMiddleware:
    def __init__(self,app):self.app=app
    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        client=scope.get('client')
        context=RequestAuditScope(client[0] if client else None)
        scope[_SCOPE_KEY]=context
        async def wrapped_send(message):
            if message['type']=='http.response.body' and not message.get('more_body',False):context.clear()
            await send(message)
        async def wrapped_receive():
            message=await receive()
            if message['type']=='http.disconnect':context.clear()
            return message
        try:await self.app(scope,wrapped_receive,wrapped_send)
        finally:
            context.clear()
            scope.pop(_SCOPE_KEY,None)
