"""Actual owned descriptor-relative no-overwrite rename; not retirement wiring."""
import importlib
import os
import pytest

def module():return importlib.import_module('core.timelapse_rename')

@pytest.fixture
def directories(tmp_path):
    source=tmp_path/'source';destination=tmp_path/'destination';source.mkdir();destination.mkdir()
    descriptors=[os.open(path,os.O_RDONLY|os.O_DIRECTORY) for path in [source,destination]]
    try:yield source,destination,*descriptors
    finally:
        for descriptor in descriptors:os.close(descriptor)

def test_native_move_is_descriptor_relative(directories):
    source,destination,source_fd,destination_fd=directories
    (source/'movie').write_bytes(b'owned movie')
    module().rename_noreplace(source_fd,'movie',destination_fd,'planned')
    assert not (source/'movie').exists()
    assert (destination/'planned').read_bytes()==b'owned movie'

def test_existing_destination_is_never_overwritten(directories):
    source,destination,source_fd,destination_fd=directories
    (source/'movie').write_bytes(b'owned movie');(destination/'planned').write_bytes(b'owned existing')
    with pytest.raises(FileExistsError):module().rename_noreplace(source_fd,'movie',destination_fd,'planned')
    assert (source/'movie').read_bytes()==b'owned movie'
    assert (destination/'planned').read_bytes()==b'owned existing'

@pytest.mark.parametrize('name',['','..','.','../escape','/escape','a/b','a\\b','a\x00b'])
def test_invalid_component_refuses_before_move(directories,name):
    source,destination,source_fd,destination_fd=directories
    (source/'movie').write_bytes(b'owned movie')
    implementation=module()
    with pytest.raises(implementation.RenameUnavailable):implementation.rename_noreplace(source_fd,'movie',destination_fd,name)
    assert (source/'movie').exists()

@pytest.mark.parametrize('descriptor',[-1,True,1.5,2**40])
def test_invalid_descriptor_refuses(directories,descriptor):
    implementation=module()
    with pytest.raises(implementation.RenameUnavailable):implementation.rename_noreplace(descriptor,'movie',directories[3],'planned')

def test_destination_inode_and_source_inode_survive_collision(directories):
    source,destination,source_fd,destination_fd=directories
    (source/'movie').write_bytes(b'owned movie');(destination/'planned').write_bytes(b'owned existing')
    before=(os.stat(source/'movie').st_ino,os.stat(destination/'planned').st_ino)
    with pytest.raises(FileExistsError):module().rename_noreplace(source_fd,'movie',destination_fd,'planned')
    assert (os.stat(source/'movie').st_ino,os.stat(destination/'planned').st_ino)==before
    assert os.fstat(source_fd) and os.fstat(destination_fd)

def test_same_name_alias_descriptor_is_refused(directories):
    source,destination,source_fd,destination_fd=directories
    implementation=module();alias=os.dup(source_fd)
    try:
        with pytest.raises(implementation.RenameUnavailable):implementation.rename_noreplace(source_fd,'movie',alias,'movie')
    finally:os.close(alias)

@pytest.mark.parametrize('error',['ENOSYS','EINVAL','ENOTSUP'])
def test_unsupported_native_operation_has_no_ordinary_rename_fallback(directories,monkeypatch,error):
    import ctypes,errno
    implementation=module()
    source,destination,source_fd,destination_fd=directories
    (source/'movie').write_bytes(b'owned movie')
    def unavailable(*args):ctypes.set_errno(getattr(errno,error));return -1
    monkeypatch.setattr(implementation,'_native',lambda:(unavailable,1))
    monkeypatch.setattr(implementation.os,'rename',lambda *args,**kwargs:pytest.fail('No ordinary rename fallback'))
    with pytest.raises(implementation.RenameUnavailable):implementation.rename_noreplace(source_fd,'movie',destination_fd,'planned')
    assert (source/'movie').read_bytes()==b'owned movie' and not (destination/'planned').exists()

def test_file_descriptor_cannot_be_used_as_directory(directories):
    source,destination,source_fd,destination_fd=directories
    implementation=module();(source/'movie').write_bytes(b'owned movie')
    descriptor=os.open(source/'movie',os.O_RDONLY)
    try:
        with pytest.raises(implementation.RenameUnavailable):implementation.rename_noreplace(descriptor,'movie',destination_fd,'planned')
    finally:os.close(descriptor)

@pytest.mark.parametrize('name',['movie','x'*256])
def test_bad_source_or_destination_component_preserves_source(directories,name):
    implementation=module();source,destination,source_fd,destination_fd=directories
    (source/'movie').write_bytes(b'owned movie')
    with pytest.raises(implementation.RenameUnavailable):implementation.rename_noreplace(source_fd,'../movie' if name=='movie' else 'movie',destination_fd,name)
    assert (source/'movie').read_bytes()==b'owned movie'

def test_unencodable_name_uses_fixed_refusal(directories):
    implementation=module()
    with pytest.raises(implementation.RenameUnavailable) as refused:
        implementation.rename_noreplace(directories[2],'owned\ud800private',directories[3],'planned')
    assert str(refused.value)=='Native no-replace move is unavailable'
