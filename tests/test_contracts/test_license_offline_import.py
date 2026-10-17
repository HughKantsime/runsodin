"""Owned actual offline import handler, with genuine ephemeral signatures."""
import asyncio
import base64
import io
import json
from unittest.mock import Mock
import pytest
from fastapi import HTTPException, UploadFile
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

@pytest.fixture
def owned_import(tmp_path, monkeypatch):
    import license_manager as lm
    from modules.system import routes_health as routes
    key=Ed25519PrivateKey.generate()
    monkeypatch.setattr(lm,'ODIN_PUBLIC_KEY',key.public_key().public_bytes(Encoding.PEM,PublicFormat.SubjectPublicKeyInfo).decode())
    monkeypatch.setattr(lm,'LICENSE_DIR',str(tmp_path))
    monkeypatch.setattr(lm,'_find_license_file',lambda:str(tmp_path/'odin.license'))
    monkeypatch.delenv('ODIN_REQUIRE_LICENSE_BINDING',raising=False)
    monkeypatch.setattr(lm,'_cached_license',None)
    monkeypatch.setattr(lm,'_cached_mtime',0)
    monkeypatch.setattr(routes,'get_license',lm.load_license)
    monkeypatch.setattr(routes,'save_license_file',lm.save_license_file)
    audit=Mock()
    monkeypatch.setattr(routes,'log_audit',audit)
    db=Mock()
    payload=json.dumps({'tier':'education','licensee':'Owned synthetic school','expires_at':'2099-01-01','features':['education_workflows']}).encode()
    signed=base64.urlsafe_b64encode(payload).decode()+'.'+base64.urlsafe_b64encode(key.sign(payload)).decode()
    path=tmp_path/'odin.license'
    path.write_text(signed)
    def upload(content):
        return asyncio.run(routes.upload_license(UploadFile(filename='synthetic.license',file=io.BytesIO(content)),current_user={'id':1,'role':'admin','group_id':None},db=db,license_mutation_allowed=None))
    def sign(**overrides):
        raw=json.dumps({'tier':'education','licensee':'Owned synthetic school','expires_at':'2099-01-01','features':['education_workflows'],**overrides}).encode()
        return (base64.urlsafe_b64encode(raw).decode()+'.'+base64.urlsafe_b64encode(key.sign(raw)).decode()).encode()
    return upload,path,signed,audit,db,lm,sign

def test_json_file_activates_genuine_signed_license(owned_import):
    upload,path,signed,audit,db,lm,sign=owned_import
    payload,signature=signed.split('.')
    result=upload(json.dumps({'payload':payload,'signature':signature}).encode())
    assert result['status']=='activated' and result['tier']=='education'
    assert path.read_text()==signed and lm.load_license().valid
    audit.assert_called_once();db.commit.assert_called_once()

@pytest.mark.parametrize('content',[b'e30=.AAAA',b'\xff',b'{"payload":{},"signature":[]}',b'[]'])
def test_invalid_replacement_preserves_installed_license(owned_import,content):
    upload,path,signed,audit,db,lm,sign=owned_import
    with pytest.raises(HTTPException) as exc:
        upload(content)
    assert exc.value.status_code==400
    assert path.read_text()==signed and lm.load_license().valid
    audit.assert_not_called();db.commit.assert_not_called()

@pytest.mark.parametrize('overrides',[{'expires_at':'2000-01-01'},{'installation_id':'synthetic-different-installation'}])
def test_invalid_signed_entitlement_preserves_installed_license(owned_import,overrides):
    upload,path,signed,audit,db,lm,sign=owned_import
    with pytest.raises(HTTPException) as exc:
        upload(sign(**overrides))
    assert exc.value.status_code==400
    assert path.read_text()==signed and lm.load_license().valid
    audit.assert_not_called();db.commit.assert_not_called()

def test_compact_signed_import_activates(owned_import):
    upload,path,signed,audit,db,lm,sign=owned_import
    result=upload(sign(max_users=374))
    assert result['status']=='activated' and lm.load_license().max_users==374
    audit.assert_called_once();db.commit.assert_called_once()

def test_rejected_candidate_does_not_create_installation_id(owned_import):
    upload,path,signed,audit,db,lm,sign=owned_import
    with pytest.raises(HTTPException):
        upload(sign(installation_id='synthetic-unestablished-installation'))
    assert not (path.parent/lm.INSTALL_ID_FILENAME).exists()
    assert path.read_text()==signed

def test_oversize_upload_preserves_license(owned_import):
    upload,path,signed,audit,db,lm,sign=owned_import
    with pytest.raises(HTTPException) as exc:
        upload(b'x'*(1024*1024+1))
    assert exc.value.status_code==400 and path.read_text()==signed
    audit.assert_not_called();db.commit.assert_not_called()

def test_replace_failure_preserves_license_and_removes_staging(owned_import,monkeypatch):
    upload,path,signed,audit,db,lm,sign=owned_import
    def fail(*args): raise OSError('Synthetic replacement refusal')
    monkeypatch.setattr(lm.os,'replace',fail)
    with pytest.raises(HTTPException) as exc: upload(sign(max_users=374))
    assert exc.value.status_code==503 and path.read_text()==signed
    assert not list(path.parent.glob('.odin-license-*.tmp'))
    audit.assert_not_called();db.commit.assert_not_called()

def test_directory_sync_failure_is_unconfirmed_without_restoring_old_bytes(owned_import,monkeypatch):
    import stat
    upload,path,signed,audit,db,lm,sign=owned_import
    original=lm.os.fsync
    def fail_directory(fd):
        if stat.S_ISDIR(lm.os.fstat(fd).st_mode): raise OSError('Synthetic directory sync refusal')
        return original(fd)
    monkeypatch.setattr(lm.os,'fsync',fail_directory)
    monkeypatch.setattr(lm,'_cached_license',lm.load_license())
    candidate=sign(max_users=374)
    with pytest.raises(HTTPException) as exc: upload(candidate)
    assert exc.value.status_code==503 and path.read_bytes()==candidate
    assert lm._cached_license is None and lm.load_license().max_users==374
    assert not list(path.parent.glob('.odin-license-*.tmp'))
    audit.assert_not_called();db.commit.assert_not_called()

def test_two_overlapping_saves_have_distinct_private_staging(owned_import,monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    upload,path,signed,audit,db,lm,sign=owned_import
    original=lm.os.replace
    barrier=threading.Barrier(2)
    staging=[]
    def simultaneous(source,target):
        staging.append(source)
        assert lm.os.stat(source).st_mode & 0o777==0o600
        barrier.wait(timeout=5)
        return original(source,target)
    monkeypatch.setattr(lm.os,'replace',simultaneous)
    candidates=[sign(max_users=2).decode(),sign(max_users=374).decode()]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lm.save_license_file,candidates))
    assert len(set(staging))==2 and results==[str(path),str(path)]
    assert path.read_text() in candidates and lm.load_license().valid
    assert not list(path.parent.glob('.odin-license-*.tmp'))

@pytest.mark.parametrize('principal',[
    {'role':'admin','group_id':None,'_auth_kind':'user_api_token','_token_scopes':['read']},
    {'role':'admin','group_id':None,'_auth_kind':'user_api_token','_token_scopes':[]},
    {'role':'admin','group_id':7},
    {'role':'viewer','group_id':None},
])
def test_http_import_requires_system_admin_write_authority(owned_import,principal):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.dependencies import get_current_user
    from core.db import get_db
    from modules.system import routes_health
    upload,path,signed,audit,db,lm,sign=owned_import
    app=FastAPI();app.add_api_route('/license/upload',routes_health.upload_license,methods=['POST'])
    app.dependency_overrides[get_current_user]=lambda:{'id':1,**principal}
    app.dependency_overrides[get_db]=lambda:db
    with TestClient(app) as client:
        response=client.post('/license/upload',files={'file':('synthetic.license',sign(max_users=374),'application/octet-stream')})
    assert response.status_code==403
    assert path.read_text()==signed
    audit.assert_not_called();db.commit.assert_not_called()

@pytest.mark.parametrize('managed',[False,True])
def test_http_write_admin_import_respects_managed_license_guard(owned_import,monkeypatch,managed):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.dependencies import get_current_user
    from core.db import get_db
    from modules.system import routes_health
    upload,path,signed,audit,db,lm,sign=owned_import
    monkeypatch.setenv('ODIN_LICENSE_READ_ONLY','1' if managed else '0')
    app=FastAPI();app.add_api_route('/license/upload',routes_health.upload_license,methods=['POST'])
    app.dependency_overrides[get_current_user]=lambda:{'id':1,'role':'admin','group_id':None,'_auth_kind':'user_api_token','_token_scopes':['write']}
    app.dependency_overrides[get_db]=lambda:db
    with TestClient(app) as client:
        response=client.post('/license/upload',files={'file':('synthetic.license',sign(max_users=374),'application/octet-stream')})
    assert response.status_code==(403 if managed else 200)
    if managed:
        assert path.read_text()==signed
        audit.assert_not_called();db.commit.assert_not_called()
    else:
        assert lm.load_license().max_users==374
        audit.assert_called_once();db.commit.assert_called_once()


def test_deep_json_returns_fixed_refusal_without_mutation(owned_import):
    upload,path,signed,audit,db,lm,sign=owned_import
    with pytest.raises(HTTPException) as exc: upload(b'['*2000+b'0'+b']'*2000)
    assert exc.value.status_code==400 and path.read_text()==signed
    audit.assert_not_called();db.commit.assert_not_called()

@pytest.mark.parametrize('failure',['audit','commit','commit_and_rollback'])
def test_audit_failure_reports_unconfirmed_without_restoring_license(owned_import,failure):
    upload,path,signed,audit,db,lm,sign=owned_import
    if failure=='audit': audit.side_effect=RuntimeError('Synthetic audit failure')
    else: db.commit.side_effect=RuntimeError('Synthetic uncertain commit')
    if failure=='commit_and_rollback': db.rollback.side_effect=RuntimeError('Synthetic rollback failure')
    candidate=sign(max_users=374)
    with pytest.raises(HTTPException) as exc: upload(candidate)
    assert exc.value.status_code==503
    assert exc.value.detail=='License installation could not be confirmed; check current license status'
    assert path.read_bytes()==candidate and lm.load_license().max_users==374
    db.rollback.assert_called_once()


@pytest.mark.parametrize('tier', ['pro', 'education', 'enterprise'])
def test_portal_null_feature_list_means_tier_default(owned_import, tier):
    """F20-20.3-09: the portal signs "features": null for a blank feature list."""
    upload,path,signed,audit,db,lm,sign=owned_import
    result=upload(sign(tier=tier, features=None))
    assert result['status']=='activated' and result['tier']==tier
    info=lm.load_license()
    assert info.valid and info.error is None
    # O14: null means the tier defaults, education_workflows included for Education/Enterprise.
    assert info.to_dict()['features']==sorted(lm.TIERS[tier]['features'])
    assert info.has_feature('education_workflows') is (tier in ('education', 'enterprise'))
    assert info.has_feature('usage_reports')


@pytest.mark.parametrize('features', ['audit_export', {'audit_export': True}, [1, 2], ['audit_export', None]])
def test_malformed_feature_list_is_rejected_and_installed_license_kept(owned_import, features):
    upload,path,signed,audit,db,lm,sign=owned_import
    with pytest.raises(HTTPException) as exc:
        upload(sign(tier='enterprise', features=features))
    assert exc.value.status_code==400
    assert path.read_text()==signed and lm.load_license().valid
