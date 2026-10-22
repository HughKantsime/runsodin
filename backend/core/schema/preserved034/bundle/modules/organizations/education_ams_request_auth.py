"""Recheck the same in-memory request credential before teacher read persistence.

Authenticate BEFORE service locks: ordinary auth may commit last-seen updates.
This is a current auth check, not atomic exclusion of later revocation.
"""
from core.dependencies import get_current_user, oauth2_scheme
from modules.organizations.education_access import require_education_principal


class EducationAMSRequestUnavailable(ValueError):
    pass


async def reauthenticate_teacher_request(request, *, expected_principal, db):
    """Explicit dependency arguments; no request credential stored or logged."""
    try:
        if type(expected_principal) is not dict:
            raise ValueError()
        token=await oauth2_scheme(request)
        current=await get_current_user(request=request,token=token,db=db)
        current=await require_education_principal(write=True)(current_user=current)
        for key in ('id','group_id','_auth_kind'):
            expected=expected_principal.get(key)
            actual=current.get(key)
            if type(expected) is not type(actual) or expected!=actual or expected is None:
                raise ValueError()
        if current['_auth_kind']=='user_api_token':
            before=expected_principal.get('_token_id')
            after=current.get('_token_id')
            if type(before) is not int or before<=0 or type(after) is not int or before!=after:
                raise ValueError()
        return current
    except Exception:
        raise EducationAMSRequestUnavailable('Education read authorization unavailable') from None
