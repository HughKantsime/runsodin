"""All runtime user creation goes through this capacity-checked insert."""
from sqlalchemy import text
from core.db_compat import execute_insert_returning_id
from core.license_capacity import lock_capacity


def create_user_record(db, *, username, email, password_hash, role, group_id=None,
                       is_active=True, oidc_subject=None, oidc_provider=None,
                       oidc_issuer=None, last_login=None):
    from license_manager import check_user_limit
    lock_capacity(db, "users")
    check_user_limit(db.execute(text("SELECT COUNT(*) FROM users")).scalar())
    return execute_insert_returning_id(db,
        "INSERT INTO users (username,email,password_hash,role,group_id,is_active,"
        "oidc_subject,oidc_provider,oidc_issuer,last_login) VALUES "
        "(:username,:email,:password_hash,:role,:group_id,:is_active,"
        ":oidc_subject,:oidc_provider,:oidc_issuer,:last_login)",
        {"username": username, "email": email, "password_hash": password_hash,
         "role": role, "group_id": group_id, "is_active": is_active,
         "oidc_subject": oidc_subject, "oidc_provider": oidc_provider,
         "oidc_issuer": oidc_issuer, "last_login": last_login})
