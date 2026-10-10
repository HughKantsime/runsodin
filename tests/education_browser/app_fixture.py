"""Disposable HTTP/browser fixture. Never use this test issuer for a deployed pilot."""
import base64
import json
import os
import secrets
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'backend'))


def main():
    directory = Path(sys.argv[1]).resolve()
    port = int(sys.argv[2])
    directory.mkdir(parents=True, exist_ok=True)
    from cryptography.fernet import Fernet
    os.environ.update(ENCRYPTION_KEY=Fernet.generate_key().decode(), AUTO_DISPATCH='false',
                      LICENSE_SERVER_URL='http://127.0.0.1:1',
                      DATABASE_URL=f"sqlite:///{directory / 'review.db'}",
                      DATABASE_PATH=str(directory / 'review.db'),
                      JWT_SECRET_KEY=secrets.token_urlsafe(48),
                      COOKIE_SECURE='false', API_KEY='', CORS_ORIGINS=f'http://127.0.0.1:{port}',
                      TRUSTED_HOSTS='127.0.0.1,localhost',
                      LICENSE_DIR=str(directory / 'license'),
                      EDUCATION_UPLOAD_ROOT=str(directory / 'uploads'),
                      EDUCATION_MIN_FREE_GIB='1')
    from tests.test_contracts.test_education_review_workflow import review_db
    fixture = review_db.__wrapped__(directory)
    db = next(fixture)
    from core.auth import hash_password
    from sqlalchemy import text
    with db.get_bind().begin() as connection:
        connection.execute(text('UPDATE users SET password_hash=:hash'), {'hash': hash_password(os.environ['ODIN_EDU_TEST_PASSWORD'])})
        connection.execute(text("INSERT INTO system_config (key,value) VALUES ('education_mode','true'),('setup_complete','true')"))
        connection.execute(text('UPDATE users SET role=\'operator\' WHERE id=3'))
        connection.execute(text('INSERT INTO users (id,username,email,password_hash,role,is_active,group_id) SELECT 6,\'superadmin\',\'superadmin@example.test\',password_hash,\'admin\',1,NULL FROM users WHERE id=1'))
        connection.execute(text("INSERT INTO users (id,username,email,password_hash,role,is_active,group_id) SELECT 7,'student-two','student-two@example.test',password_hash,'viewer',1,1 FROM users WHERE id=1"))
        connection.execute(text("INSERT INTO education_cost_center_grants (org_id,cost_center_id,user_id,role,state,granted_by) VALUES (1,7,7,'student','active',6)"))
    with db.get_bind().begin() as connection:
        for table, column, value in [('education_submissions','id',14),('jobs','id',13),('models','id',12),('print_files','id',11),('education_upload_operations','operation_id','upload-1')]:
            connection.execute(text(f'DELETE FROM {table} WHERE {column}=:value'), {'value': value})
    import license_manager
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    key = Ed25519PrivateKey.generate()
    license_manager.ODIN_PUBLIC_KEY = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
    payload = json.dumps({'tier':'education', 'licensee':'Synthetic Browser Test', 'email':'test@example.test', 'expires_at':'2099-12-31', 'max_printers':10, 'max_users':20, 'features':license_manager.TIERS['education']['features']}, sort_keys=True).encode()
    license_directory = Path(os.environ['LICENSE_DIR'])
    license_directory.mkdir()
    (license_directory / 'odin.license').write_text(base64.urlsafe_b64encode(payload).decode()+'.'+base64.urlsafe_b64encode(key.sign(payload)).decode())
    license_manager._cached_license = None
    assert license_manager.get_license().valid
    from core.app import create_app
    import uvicorn
    # Lifespan is disabled to exclude unrelated camera/background services.
    # Route discovery, actual JWT/password authentication and HTTP handlers are real.
    try:
        uvicorn.run(create_app(), host='127.0.0.1', port=port, lifespan='off', access_log=False)
    finally:
        fixture.close()


if __name__ == '__main__':
    main()
