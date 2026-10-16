"""Camera-stack review LOW 6: the credential write guard covers Classroom OAuth material and
MFA enrolments, and PUT /education/classroom/config needs an interactive session."""
import pytest
from fastapi import HTTPException


@pytest.mark.parametrize("statement", [
    "INSERT INTO classroom_connections (org_id, client_id, client_secret_encrypted, allowed_domains, state, created_by) "
    "VALUES (:o, :c, :s, :d, 'not_connected', :a) ON CONFLICT (org_id) DO UPDATE SET client_secret_encrypted=:s",
    "UPDATE classroom_connections SET account_subject=:subject, account_email=:email, refresh_token_encrypted=:r WHERE org_id=:o",
    "INSERT INTO classroom_oauth_states (state, org_id, admin_id, code_verifier, expires_at) VALUES (:s, :o, :a, :v, :e)",
    "INSERT INTO mfa_enrollments(jti,user_id,expires_at,state,created_at) VALUES(:jti,:uid,:exp,'pending',:now)",
    "UPDATE mfa_enrollments SET state='consumed', encrypted_pending_secret=NULL WHERE jti=:jti",
])
def test_api_token_writes_to_classroom_and_mfa_material_are_refused(statement):
    from core import credential_write_guard as guard
    token = guard._NONINTERACTIVE.set(True)
    try:
        with pytest.raises(HTTPException) as refused:
            guard.check_credential_write(statement, {})
        assert refused.value.status_code == 403
    finally:
        guard._NONINTERACTIVE.reset(token)
    guard.check_credential_write(statement, {})  # an interactive session is unaffected


@pytest.mark.parametrize("statement", [
    "UPDATE classroom_connections SET access_token_encrypted=:access, access_token_expires_at=:expires, "
    "last_success_at=:now, last_error_code=NULL, updated_at=CURRENT_TIMESTAMP WHERE org_id=:org_id",
    "UPDATE classroom_connections SET state='reconnect_required', last_error_code='classroom_refresh_failed', "
    "updated_at=CURRENT_TIMESTAMP WHERE org_id=:org_id",
    "DELETE FROM classroom_oauth_states WHERE expires_at<:now",
    "DELETE FROM mfa_enrollments WHERE user_id = :uid",
])
def test_token_refresh_and_withdrawals_stay_allowed(statement):
    """Reading courses with a token refreshes ODIN's own Classroom access token; deletes withdraw access."""
    from core import credential_write_guard as guard
    token = guard._NONINTERACTIVE.set(True)
    try:
        guard.check_credential_write(statement, {})
    finally:
        guard._NONINTERACTIVE.reset(token)


def test_the_refresh_allowance_matches_the_service_statement_exactly():
    import inspect
    from core import credential_write_guard as guard
    from modules.organizations import classroom_service
    source = inspect.getsource(classroom_service._access_token)
    assert "access_token_encrypted=:access" in source and "refresh_token_encrypted=" not in source.split("UPDATE")[1].split("WHERE")[0]
    assert guard.guarded_write_target("UPDATE classroom_connections SET access_token_encrypted=:a, refresh_token_encrypted=:r, "
                                      "access_token_expires_at=:e, last_success_at=:n, last_error_code=NULL, "
                                      "updated_at=CURRENT_TIMESTAMP WHERE org_id=:o", {}) == "classroom_connections"


def test_classroom_config_route_requires_an_interactive_session():
    from tests.test_contracts.test_cert_F_B_credential_census import _interactive_routes
    interactive = _interactive_routes()
    assert any(method == "PUT" and path.endswith("/education/classroom/config") for method, path in interactive), \
        sorted(r for r in interactive if "classroom" in r[1])
