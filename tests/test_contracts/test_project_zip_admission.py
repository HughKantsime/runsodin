"""Actual ZIP bytes reject corruption, unexpected members and unsafe modes."""
import hashlib,io,json,stat,struct,zipfile
from copy import deepcopy
import pytest
from modules.archives.project_zip_admission import validate_project_zip
from tests.test_contracts.test_project_zip_manifest import manifest


def package(*,metadata=None,content=b'owned',extra=None,mode=None,compression=zipfile.ZIP_STORED):
    value=deepcopy(metadata if metadata is not None else manifest())
    if metadata is None:
        value['archives'][0]['file'].update(size=len(content),sha256=hashlib.sha256(content).hexdigest())
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',compression=compression) as archive:
        archive.writestr('project.json',json.dumps(value))
        info=zipfile.ZipInfo('files/000.3mf')
        if mode is not None:info.external_attr=mode<<16;info.create_system=3
        archive.writestr(info,content)
        if extra is not None:archive.writestr(extra,b'unexpected')
    return out.getvalue()


@pytest.mark.parametrize('compression',[zipfile.ZIP_STORED,zipfile.ZIP_DEFLATED])
def test_real_closed_zip_counts_and_hashes_without_file_effects(compression):
    payload=package(compression=compression)
    result=validate_project_zip(payload)
    assert result.total_file_bytes==5
    assert result.package_sha256==hashlib.sha256(payload).hexdigest()
    assert result.manifest.archives[0].protection=='education'


@pytest.mark.parametrize('name',['../outside','/absolute','files\\000.3mf','files/001.3mf','project.json','folder/'])
def test_unexpected_traversal_duplicate_or_directory_members_refuse(name):
    with pytest.raises(ValueError):validate_project_zip(package(extra=name))


@pytest.mark.parametrize('mode',[stat.S_IFLNK|0o777,stat.S_IFIFO|0o600,stat.S_IFDIR|0o700])
def test_special_unix_entries_refuse(mode):
    with pytest.raises(ValueError):validate_project_zip(package(mode=mode))


@pytest.mark.parametrize('change',['wrong_hash','wrong_size','missing_member'])
def test_manifest_integrity_differs_from_actual_bytes(change):
    value=manifest();file=value['archives'][0]['file']
    if change=='wrong_hash':file.update(size=5,sha256='f'*64)
    elif change=='wrong_size':file.update(size=6,sha256=hashlib.sha256(b'owned').hexdigest())
    else:
        file.update(size=5,sha256=hashlib.sha256(b'owned').hexdigest())
        out=io.BytesIO()
        with zipfile.ZipFile(out,'w') as archive:archive.writestr('project.json',json.dumps(value))
        with pytest.raises(ValueError):validate_project_zip(out.getvalue())
        return
    with pytest.raises(ValueError):validate_project_zip(package(metadata=value))


def test_payload_crc_corruption_refuses():
    payload=bytearray(package());offset=payload.find(b'owned');assert offset>=0
    payload[offset]^=1
    with pytest.raises(ValueError):validate_project_zip(bytes(payload))


def test_encryption_flag_refuses_without_password_attempt():
    payload=bytearray(package());start=0
    while True:
        position=payload.find(b'PK\x01\x02',start)
        if position<0:break
        flags=struct.unpack_from('<H',payload,position+8)[0]
        struct.pack_into('<H',payload,position+8,flags|1);start=position+4
    with pytest.raises(ValueError):validate_project_zip(bytes(payload))


def test_deadline_expires_before_member_validation():
    values=iter([0,61])
    with pytest.raises(ValueError):validate_project_zip(package(),clock=lambda:next(values))


def test_declared_zip_bomb_size_refuses_before_decompress():
    payload=bytearray(package());position=payload.find(b'PK\x01\x02')
    struct.pack_into('<I',payload,position+24,1024*1024+1)
    with pytest.raises(ValueError):validate_project_zip(bytes(payload))


def test_many_entries_refuse_before_zipfile_inventory_allocation(monkeypatch):
    from modules.archives import project_zip_admission as admission
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as archive:
        for index in range(1000):archive.writestr(f'files/{index:03d}.3mf',b'')
    def forbidden(*args,**kwargs):raise AssertionError('ZipFile allocated an unbounded inventory')
    monkeypatch.setattr(admission.zipfile,'ZipFile',forbidden)
    with pytest.raises(ValueError):admission.validate_project_zip(out.getvalue())


def test_inconsistent_directory_count_refuses_before_zipfile(monkeypatch):
    from modules.archives import project_zip_admission as admission
    raw=bytearray(package());end=raw.rfind(b'PK\x05\x06')
    struct.pack_into('<H',raw,end+10,1)
    def forbidden(*args,**kwargs):raise AssertionError('ZipFile parsed inconsistent directory')
    monkeypatch.setattr(admission.zipfile,'ZipFile',forbidden)
    with pytest.raises(ValueError):admission.validate_project_zip(bytes(raw))


def test_malformed_deflate_is_normalized_to_refusal():
    out=io.BytesIO();value=manifest();value['archives'][0]['file'].update(size=5,sha256=hashlib.sha256(b'owned').hexdigest())
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('project.json',json.dumps(value));archive.writestr('files/000.3mf',b'owned')
    raw=bytearray(out.getvalue())
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:info=archive.getinfo('files/000.3mf')
    local=info.header_offset;name,extra=struct.unpack_from('<2H',raw,local+26);start=local+30+name+extra
    raw[start]=0xff
    with pytest.raises(ValueError):validate_project_zip(bytes(raw))


def test_small_force_zip64_is_explicitly_unsupported_before_inventory(monkeypatch):
    from modules.archives import project_zip_admission as admission
    value=manifest();value['archives'][0]['file'].update(size=5,sha256=hashlib.sha256(b'owned').hexdigest())
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as archive:
        archive.writestr('project.json',json.dumps(value))
        with archive.open('files/000.3mf','w',force_zip64=True) as source:source.write(b'owned')
    def forbidden(*args,**kwargs):raise AssertionError('Unsupported ZIP64 constructed inventory')
    monkeypatch.setattr(admission.zipfile,'ZipFile',forbidden)
    with pytest.raises(ValueError):admission.validate_project_zip(out.getvalue())
