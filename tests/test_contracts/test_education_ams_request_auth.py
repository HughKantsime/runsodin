"""Real credentials rechecked after completed revocation/account mutations."""
import pytest
from fastapi import Depends,Request
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_api_token_delegation_behavior import token_client,review_db,interactive_headers


@pytest.mark.parametrize('change', ['none','disabled','tenant','revoked_api','scopes'])
def test_request_bound_api_credential_recheck(token_client,review_db,monkeypatch,change):
    from modules.organizations import education_access
    from modules.organizations.education_ams_request_auth import reauthenticate_teacher_request,EducationAMSRequestUnavailable
    monkeypatch.setattr(education_access,'require_education_license',lambda:None)
    client,issue=token_client
    @client.app.post('/synthetic-reauth')
    async def protected(request:Request,principal=Depends(education_access.require_education_principal(write=True))):
        def mutate():  # a concurrent admin, not this token request (fresh context)
            with Session(review_db.get_bind()) as mutation:
                if change=='disabled':mutation.execute(text('UPDATE users SET is_active=0 WHERE id=3'))
                elif change=='tenant':mutation.execute(text('UPDATE users SET group_id=2 WHERE id=3'))
                elif change=='revoked_api':mutation.execute(text('DELETE FROM api_tokens WHERE id=:id'),{'id':principal['_token_id']})
                elif change=='scopes':mutation.execute(text("UPDATE api_tokens SET scopes='[\"read:education\"]' WHERE id=:id"),{'id':principal['_token_id']})
                mutation.commit()
        import contextvars
        contextvars.Context().run(mutate)
        with Session(review_db.get_bind()) as fresh:
            try:
                current=await reauthenticate_teacher_request(request,expected_principal=principal,db=fresh)
            except EducationAMSRequestUnavailable:return {'allowed':False}
            return {'allowed':True,'same_user':current['id']==principal['id']}
    response=client.post('/synthetic-reauth',headers=issue(['write:education']))
    assert response.status_code==200
    assert response.json()==({'allowed':True,'same_user':True} if change=='none' else {'allowed':False})


@pytest.mark.parametrize('revoke',[False,True])
def test_request_bound_jwt_recheck(token_client,review_db,monkeypatch,revoke):
    import jwt
    from core import auth
    from modules.organizations import education_access
    from modules.organizations.education_ams_request_auth import reauthenticate_teacher_request,EducationAMSRequestUnavailable
    monkeypatch.setattr(education_access,'require_education_license',lambda:None)
    client,_=token_client
    @client.app.post('/synthetic-jwt-reauth')
    async def protected(request:Request,principal=Depends(education_access.require_education_principal(write=True))):
        if revoke:
            raw=request.headers['authorization'].split(' ',1)[1]
            payload=jwt.decode(raw,auth.SECRET_KEY,algorithms=[auth.ALGORITHM])
            with Session(review_db.get_bind()) as mutation:
                mutation.execute(text('INSERT INTO token_blacklist(jti,expires_at) VALUES(:jti,:expires)'),
                    {'jti':payload['jti'],'expires':'2099-01-01 00:00:00'})
                mutation.commit()
        with Session(review_db.get_bind()) as fresh:
            try:await reauthenticate_teacher_request(request,expected_principal=principal,db=fresh)
            except EducationAMSRequestUnavailable:return {'allowed':False}
            return {'allowed':True}
    response=client.post('/synthetic-jwt-reauth',headers=interactive_headers())
    assert response.status_code==200
    assert response.json()=={'allowed':not revoke}
