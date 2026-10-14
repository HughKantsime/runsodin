"""Real stateless Archives adapter; original helpers own authority, transactions and FDs."""

class TimelapseMediaService:
    def capture_action_credential(self,request,db,credential,*,kind,carrier,purpose,selected):
        from modules.archives.timelapse_action_authority import capture_action_credential
        return capture_action_credential(request,db,credential,kind=kind,carrier=carrier,purpose=purpose,selected=selected)

    def admitted_response(self,engine,root,tid,evidence,*,range_header=None,head=False,filename=None,expected_nonce=None):
        from modules.archives.timelapse_read_lease import admitted_response
        return admitted_response(engine,root,tid,evidence,range_header=range_header,head=head,filename=filename,expected_nonce=expected_nonce)

    def sealed_lifetime_receipt(self,engine,evidence,*,operation_id,original_id=None):
        from modules.archives.timelapse_lifetime_action import sealed_lifetime_receipt
        return sealed_lifetime_receipt(engine,evidence,operation_id=operation_id,original_id=original_id)

    def authorize_retirement(self,admission_session,evidence,tid,nonce):
        from fastapi import HTTPException
        from modules.archives.timelapse_action_authority import fence_authorization,fresh_action_principal
        from modules.archives.models import Timelapse
        from core.timelapse_access import assert_timelapse_access
        fence_authorization(admission_session);principal=fresh_action_principal(admission_session,evidence)
        media=admission_session.get(Timelapse,tid)
        if media is None:raise HTTPException(404,'Timelapse not found')
        assert_timelapse_access(admission_session,media,principal,mutation=True)
        if media.ownership_operation_id!=nonce:raise HTTPException(412,'Lifetime precondition changed')

    def prepare_request_lifetime_retirement(self,engine,root,tid,evidence,*,expected_nonce,budget):
        from modules.archives.timelapse_lifetime_prepare import prepare_request_lifetime_retirement
        return prepare_request_lifetime_retirement(engine,root,tid,evidence,expected_nonce=expected_nonce,budget=budget)

    def retire_sealed_lifetime(self,engine,root,tid,evidence,*,expected_nonce,budget):
        from modules.archives.timelapse_lifetime_action import retire_sealed_lifetime
        return retire_sealed_lifetime(engine,root,tid,evidence,expected_nonce=expected_nonce,budget=budget)

    def execute_edit(self,engine,root,tid,evidence,identifier,*,nonce=None,recipe=None,lookup=False):
        from modules.archives.timelapse_edit import execute_edit
        return execute_edit(engine,root,tid,evidence,identifier,nonce=nonce,recipe=recipe,lookup=lookup)
