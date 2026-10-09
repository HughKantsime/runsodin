from collections import namedtuple
from pathlib import Path
import pytest
from modules.organizations import education_storage as storage

Usage = namedtuple('Usage', 'total used free')

@pytest.mark.parametrize('total,free,status,reserve', [(20,9,'blocked',10),(20,10,'blocked',10),(20,11,'ready',10),(200,19,'blocked',20),(200,20,'blocked',20),(200,21,'ready',20),(5,4,'blocked',10)])
def test_readiness_reserve_boundaries(tmp_path, monkeypatch, total, free, status, reserve):
    monkeypatch.setattr(storage.shutil, 'disk_usage', lambda path: Usage(total*1024**3,(total-free)*1024**3,free*1024**3))
    result = storage.storage_readiness(tmp_path)
    assert result['status'] == status
    assert result['reserve_bytes'] == reserve*1024**3
    assert result['upload_headroom_bytes'] == max(0,free-reserve)*1024**3


def test_missing_nested_root_is_read_only(tmp_path, monkeypatch):
    root = tmp_path / 'missing' / 'uploads'
    seen=[]
    monkeypatch.setattr(storage.shutil, 'disk_usage', lambda path: seen.append(path) or Usage(20*1024**3,0,20*1024**3))
    assert storage.storage_readiness(root)['upload_directory_exists'] is False
    assert seen == [tmp_path]
    assert not root.parent.exists()


def test_unreadable_storage_is_unknown_without_path(tmp_path, monkeypatch):
    def fail(path):
        raise PermissionError('private-host-path')
    monkeypatch.setattr(storage.shutil, 'disk_usage', fail)
    result = storage.storage_readiness(tmp_path)
    assert result['status'] == 'unknown'
    assert result['free_bytes'] is None
    assert 'private-host-path' not in str(result)


@pytest.mark.parametrize('nested', [False, True])
def test_file_in_upload_path_is_not_ready(tmp_path, nested):
    invalid = tmp_path / 'regular-file'
    invalid.write_text('not a directory')
    result = storage.storage_readiness(invalid / 'uploads' if nested else invalid)
    assert result['status'] == 'unknown'
    assert result['upload_directory_exists'] is None

@pytest.mark.parametrize('value', ['', ' ', '0', '-1', '0.99', 'NaN', 'Infinity', '-Infinity', 'oops', '1e999999', '8589934592', '9'*65])
def test_invalid_override_is_fail_closed(value, tmp_path, monkeypatch):
    monkeypatch.setenv('EDUCATION_MIN_FREE_GIB', value)
    with pytest.raises(storage.StorageConfigurationError):
        storage.required_free_bytes(16*1024**3)
    assert storage.storage_readiness(tmp_path)['status'] == 'configuration_error'

@pytest.mark.parametrize('value,total,reserve', [('1',16,1.6),('2',16,2),('1',200,20),('10',16,10)])
def test_valid_override_retains_percentage_floor(value,total,reserve,tmp_path,monkeypatch):
    monkeypatch.setenv('EDUCATION_MIN_FREE_GIB',value)
    monkeypatch.setattr(storage.shutil,'disk_usage',lambda p:Usage(total*1024**3,0,total*1024**3))
    result=storage.storage_readiness(tmp_path)
    assert result['administrator_override'] is True
    assert result['reserve_bytes'] == int(reserve*1024**3)
    assert result['configured_min_free_bytes'] == int(float(value)*1024**3)

def test_missing_setting_preserves_default(monkeypatch):
    monkeypatch.delenv('EDUCATION_MIN_FREE_GIB',raising=False)
    assert storage.configured_floor_bytes() == (10*1024**3,False)


def test_fractional_floor_rounds_up_without_overflow(monkeypatch):
    monkeypatch.setenv('EDUCATION_MIN_FREE_GIB','1.0000000001')
    assert storage.configured_floor_bytes()==(1024**3+1,True)
    monkeypatch.setenv('EDUCATION_MIN_FREE_GIB','8589934591.99999999999999999999')
    with pytest.raises(storage.StorageConfigurationError): storage.configured_floor_bytes()
