"""Real filesystem and JPEG checks for the bounded frame publisher."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from pathlib import Path
import cv2
import numpy as np
import pytest

@pytest.fixture
def writer(tmp_path, monkeypatch):
    from modules.vision import frame_storage as module
    monkeypatch.setattr(module, 'VISION_FRAMES_DIR', str(tmp_path/'frames'))
    return module


def frame(value=70):
    return np.full((20, 30, 3), value, dtype=np.uint8)


def test_concurrent_frames_and_training_decode(writer):
    with ThreadPoolExecutor(max_workers=8) as pool:
        paths=list(pool.map(lambda n: writer.save_detection_frame(9,frame(n),'future_type'),range(24)))
    assert len(set(paths)) == 24
    for path in paths:
        assert cv2.imread(str(Path(writer.VISION_FRAMES_DIR)/path)).shape == (20,30,3)
    training=writer.save_training_frame(9,frame())
    assert training.startswith('9/training/') and (Path(writer.VISION_FRAMES_DIR)/training).is_file()
    files=list(Path(writer.VISION_FRAMES_DIR).rglob('*'))
    assert len([p for p in files if p.suffix=='.jpg']) == 25
    assert not any(p.suffix=='.tmp' for p in files)

@pytest.mark.parametrize('printer',[True,0,-1,'9',9.0,None])
def test_invalid_printer(writer,printer):
    with pytest.raises(ValueError): writer.save_detection_frame(printer,frame(),'spaghetti')

@pytest.mark.parametrize('name',['../escape','x/y','',None,'x'*65,'UPPER','x.jpg'])
def test_invalid_detection(writer,name):
    with pytest.raises(ValueError): writer.save_detection_frame(9,frame(),name)

@pytest.mark.parametrize('component',['root','printer','training'])
def test_symlink_rejected(writer,tmp_path,component):
    root=Path(writer.VISION_FRAMES_DIR)
    outside=tmp_path/'outside';outside.mkdir()
    if component=='root': root.symlink_to(outside,target_is_directory=True)
    else:
        root.mkdir(); (root/'9').mkdir() if component=='training' else None
        (root/'9'/'training' if component=='training' else root/'9').symlink_to(outside,target_is_directory=True)
    with pytest.raises(OSError):
        (writer.save_training_frame(9,frame()) if component=='training' else writer.save_detection_frame(9,frame(),'spaghetti'))
    assert not list(outside.iterdir())

@pytest.mark.parametrize('codec',[(False,None),(True,np.array([],dtype=np.uint8))])
def test_codec_failure(writer,monkeypatch,codec):
    monkeypatch.setattr(writer.cv2,'imencode',lambda *a,**k:codec)
    with pytest.raises(ValueError):writer.save_detection_frame(9,frame(),'spaghetti')

@pytest.mark.parametrize('operation',['write','fsync','link'])
def test_io_failure_cleans_owned_files(writer,monkeypatch,operation):
    def fail(*a,**k):raise OSError('owned injected failure')
    monkeypatch.setattr(writer.os,operation,fail)
    with pytest.raises(OSError):writer.save_detection_frame(9,frame(),'spaghetti')
    assert not list(Path(writer.VISION_FRAMES_DIR).rglob('*.jpg'))
    assert not list(Path(writer.VISION_FRAMES_DIR).rglob('*.tmp'))


def test_short_writes(writer,monkeypatch):
    original=writer.os.write
    monkeypatch.setattr(writer.os,'write',lambda fd,data:original(fd,data[:7]))
    path=writer.save_detection_frame(9,frame(),'spaghetti')
    assert cv2.imread(str(Path(writer.VISION_FRAMES_DIR)/path)).shape==(20,30,3)


def test_collision_preserves_existing(writer,monkeypatch):
    monkeypatch.setattr(writer.uuid,'uuid4',lambda:SimpleNamespace(hex='fixed'))
    monkeypatch.setattr(writer,'datetime',SimpleNamespace(now=lambda tz:SimpleNamespace(strftime=lambda f:'fixed')))
    path=writer.save_detection_frame(9,frame(),'spaghetti')
    target=Path(writer.VISION_FRAMES_DIR)/path;original=target.read_bytes()
    with pytest.raises(FileExistsError):writer.save_detection_frame(9,frame(180),'spaghetti')
    assert target.read_bytes()==original
    assert not list(target.parent.glob('*.tmp'))

@pytest.mark.parametrize('phase',['before_publish','after_publish'])
@pytest.mark.parametrize('component',['root','printer','training'])
def test_namespace_replacement_cleans_only_owned(writer,monkeypatch,component,phase):
    root=Path(writer.VISION_FRAMES_DIR)
    verify=writer._verify_namespace;calls=0
    replaced=[]
    def replace(path,directories):
        nonlocal calls
        calls+=1
        if calls==(1 if phase=='before_publish' else 2):
            target=root if component=='root' else root/'9' if component=='printer' else root/'9'/'training'
            moved=target.with_name(target.name+'-moved');target.rename(moved);target.mkdir()
            marker=target/'unrelated';marker.write_text('preserve');replaced.append((moved,marker))
        return verify(path,directories)
    monkeypatch.setattr(writer,'_verify_namespace',replace)
    with pytest.raises(OSError):
        (writer.save_training_frame(9,frame()) if component=='training' else writer.save_detection_frame(9,frame(),'spaghetti'))
    moved,marker=replaced[0]
    assert marker.read_text()=='preserve'
    assert not list(moved.rglob('*.jpg')) and not list(moved.rglob('*.tmp'))


def test_directory_fsync_failure_removes_publication(writer,monkeypatch):
    original=writer.os.fsync;calls=0
    def fail_directory(fd):
        nonlocal calls
        calls+=1
        if calls==2:raise OSError('owned directory fsync failure')
        original(fd)
    monkeypatch.setattr(writer.os,'fsync',fail_directory)
    with pytest.raises(OSError):writer.save_detection_frame(9,frame(),'spaghetti')
    assert not list(Path(writer.VISION_FRAMES_DIR).rglob('*.jpg'))
    assert not list(Path(writer.VISION_FRAMES_DIR).rglob('*.tmp'))
