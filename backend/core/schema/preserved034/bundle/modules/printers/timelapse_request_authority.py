"""Capture only the already authenticated request carrier."""
from fastapi import HTTPException
from fastapi.security.utils import get_authorization_scheme_param
from core.dependencies import validate_access_token

def selected_evidence(request,db,principal,*,provider,purpose,query_allowed=False):
    carrier=None
    if not principal and query_allowed and request.query_params.get('token'):
        credential=request.query_params['token'];principal=validate_access_token(credential,db)
        if principal:principal=dict(principal,_auth_kind='bearer_jwt');carrier='query'
    if not principal:raise HTTPException(401,'Not authenticated')
    kind=principal.get('_auth_kind')
    if carrier=='query':pass
    elif kind=='session_jwt':carrier='cookie';credential=request.cookies.get('session')
    elif kind=='bearer_jwt':
        scheme,credential=get_authorization_scheme_param(request.headers.get('authorization'));carrier='bearer'
        if scheme.lower()!='bearer':raise HTTPException(401,'Not authenticated')
    elif kind in ('user_api_token','legacy_global_api_key'):carrier='api_key';credential=request.headers.get('x-api-key')
    else:raise HTTPException(401,'Unsupported authentication')
    if purpose=='read':
        from core.rbac import has_permission, token_scope_granted
        scopes=principal.get('_token_scopes',[])
        if not has_permission(principal.get('role'),'viewer') or (
            (scopes or kind=='user_api_token') and not token_scope_granted(scopes,'read:printers')
        ):
            raise HTTPException(403,'Insufficient timelapse read permission')
    return provider.capture_action_credential(request,db,credential,kind=kind,carrier=carrier,purpose=purpose,selected=principal)
