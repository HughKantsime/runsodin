import os,time
import pytest
from core.timelapse_manifest import _root,ManifestUnavailable
from core.timelapse_media_lock import acquire_media_guard
from core.timelapse_media_worker import file_facts
from core.timelapse_publication_validation import STABLE
from modules.archives.timelapse_frame_publication import _directory
from modules.archives.timelapse_edit_source import open_proven_media

@pytest.fixture
def source(tmp_path):
    os.chmod(tmp_path,0o700);private=tmp_path/'publish_fixture';private.mkdir(mode=0o700)
    path=private/'output.mp4';path.write_bytes(b'durable fixture bytes');path.chmod(0o600)
    fd=os.open(path,os.O_RDONLY)
    expected=file_facts(fd);os.close(fd)
    with acquire_media_guard(tmp_path,1) as guard,_root(tmp_path) as (root_fd,identity):
        life={'root_'+key:getattr(identity,key) for key in STABLE}
        parent=dict(_directory(private.stat()),relative_path='publish_fixture')
        args=dict(root_fd=root_fd,root_identity=identity,guard=guard,lifetime=life,path='publish_fixture/output.mp4',
                  expected=expected,parent_facts=[parent],ctime=expected['ctime_ns'],deadline=time.monotonic()+10)
        yield path,args

def test_exact_private_durable_receipt_reopens_readonly(source):
    path,args=source
    with open_proven_media(**args) as (fd,facts):
        assert os.read(fd,100)==b'durable fixture bytes'
        assert facts==args['expected']
        with pytest.raises(OSError):os.write(fd,b'x')

def test_no_unrecorded_parent_adoption(source):
    _,args=source;args['parent_facts']=[]
    with pytest.raises(ValueError,match='parent'): 
        with open_proven_media(**args):pass

def test_changed_bytes_refused(source):
    path,args=source;path.write_bytes(b'replaced payload')
    with pytest.raises(ValueError):
        with open_proven_media(**args):pass

def test_parent_symlink_refused(source,tmp_path):
    path,args=source;path.parent.rename(tmp_path/'old');path.parent.symlink_to(tmp_path/'old',target_is_directory=True)
    with pytest.raises(ManifestUnavailable):
        with open_proven_media(**args):pass

def test_post_probe_change_refuses(source):
    path,args=source
    with pytest.raises(ValueError):
        with open_proven_media(**args):path.write_bytes(b'changed during probe')
