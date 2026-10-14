"""Read-only manifest acceptance with actual owned filesystem objects."""
import importlib
import os
import pytest


def primitive():return importlib.import_module('core.timelapse_manifest')

@pytest.fixture
def media(tmp_path):
    (tmp_path/'printer_9').mkdir()
    (tmp_path/'printer_9'/'movie.mp4').write_bytes(b'owned movie')
    (tmp_path/'199'/'frames').mkdir(parents=True)
    (tmp_path/'199'/'frames'/'b.jpg').write_bytes(b'owned b')
    (tmp_path/'199'/'frames'/'a.jpg').write_bytes(b'owned a')
    return tmp_path


def test_exact_sorted_manifest_does_not_mutate_media(media):
    result=primitive().capture_manifest(media,'printer_9/movie.mp4','199')
    assert [o.relative_path for o in result.objects]==['199','199/frames','199/frames/a.jpg','199/frames/b.jpg','printer_9/movie.mp4']
    movie=next(o for o in result.objects if o.kind=='file' and o.relative_path.endswith('.mp4'))
    assert movie.inode==os.stat(media/'printer_9'/'movie.mp4').st_ino
    assert movie.byte_count==len(b'owned movie')
    assert (media/'printer_9'/'movie.mp4').read_bytes()==b'owned movie'
    assert (media/'199'/'frames'/'a.jpg').exists()

@pytest.mark.parametrize('path',['/tmp/escape','../escape','a/../escape','a//b','./a','a\x00b','.retirement/object','.locks/object'])
def test_untrusted_relative_paths_refuse_before_walk(media,path):
    module=primitive()
    with pytest.raises(module.ManifestUnavailable):module.capture_manifest(media,path,'199')

@pytest.mark.parametrize('location',['video','frame','parent','root'])
def test_symlinks_are_never_followed(media,tmp_path,location):
    module=primitive();outside=media.parent/'owned-outside';outside.mkdir(exist_ok=True)
    (outside/'secret').write_bytes(b'owned outside')
    if location=='video':
        video=media/'printer_9'/'movie.mp4';video.unlink();video.symlink_to(outside/'secret')
    elif location=='frame':(media/'199'/'frames'/'link.jpg').symlink_to(outside/'secret')
    elif location=='parent':
        video=media/'printer_9'/'movie.mp4';video.unlink();(media/'printer_9').rmdir();(media/'printer_9').symlink_to(outside,target_is_directory=True)
    else:
        link=media.parent/'owned-root-link';link.symlink_to(media,target_is_directory=True);media=link
    with pytest.raises(module.ManifestUnavailable):module.capture_manifest(media,'printer_9/movie.mp4','199')
    assert (outside/'secret').read_bytes()==b'owned outside'


def test_hardlinked_file_is_not_exclusive_authority(media):
    module=primitive();os.link(media/'printer_9'/'movie.mp4',media/'other.mp4')
    with pytest.raises(module.ManifestUnavailable):module.capture_manifest(media,'printer_9/movie.mp4','199')


def test_missing_objects_have_no_fabricated_identity(media):
    result=primitive().capture_manifest(media,'printer_9/missing.mp4','missing-frames')
    assert len(result.objects)==2
    assert all(o.kind=='absent' and o.inode is None and o.device is None for o in result.objects)

@pytest.mark.parametrize('limits',[{'max_objects':2},{'max_depth':1}])
def test_manifest_limits_refuse_without_partial_result(media,limits):
    module=primitive()
    with pytest.raises(module.ManifestUnavailable):module.capture_manifest(media,'printer_9/movie.mp4','199',**limits)
    assert (media/'199'/'frames'/'a.jpg').exists()

@pytest.mark.parametrize('video,frames',[('199/movie.mp4','199'),('printer_9/movie.mp4','printer_9/movie.mp4')])
def test_overlapping_requested_resources_are_refused(media,video,frames):
    module=primitive()
    with pytest.raises(module.ManifestUnavailable):module.capture_manifest(media,video,frames)

@pytest.mark.parametrize('limits',[{'max_objects':True},{'max_objects':0},{'max_objects':16385},{'max_depth':True},{'max_depth':33}])
def test_limits_are_exact_bounded_positive_integers(media,limits):
    module=primitive()
    with pytest.raises(module.ManifestUnavailable):module.capture_manifest(media,'printer_9/movie.mp4','199',**limits)

def test_group_writable_root_is_not_trusted(media):
    module=primitive();media.chmod(0o770)
    try:
        with pytest.raises(module.ManifestUnavailable):module.capture_manifest(media,'printer_9/movie.mp4','199')
    finally:media.chmod(0o700)

def test_missing_parent_is_not_leaf_absence_proof(media):
    module=primitive()
    with pytest.raises(module.ManifestUnavailable):module.capture_manifest(media,'unknown-parent/movie.mp4','199')

def test_frame_root_must_be_directory(media):
    module=primitive();(media/'not-frames').write_bytes(b'owned')
    with pytest.raises(module.ManifestUnavailable):module.capture_manifest(media,'printer_9/movie.mp4','not-frames')

def test_late_changed_video_refuses_whole_manifest(media,monkeypatch):
    module=primitive();original=module._names;changed=[]
    def names(descriptor,remaining):
        result=original(descriptor,remaining)
        if not changed:
            changed.append(True);(media/'printer_9'/'movie.mp4').write_bytes(b'owned changed media')
        return result
    monkeypatch.setattr(module,'_names',names)
    with pytest.raises(module.ManifestUnavailable):module.capture_manifest(media,'printer_9/movie.mp4','199')

def test_group_writable_media_is_not_exclusive_authority(media):
    module=primitive();(media/'printer_9'/'movie.mp4').chmod(0o666)
    with pytest.raises(module.ManifestUnavailable):module.capture_manifest(media,'printer_9/movie.mp4','199')
    assert (media/'printer_9'/'movie.mp4').read_bytes()==b'owned movie'

def test_fifo_is_refused_without_opening_or_blocking(media):
    module=primitive();os.mkfifo(media/'199'/'frames'/'owned-pipe')
    with pytest.raises(module.ManifestUnavailable):module.capture_manifest(media,'printer_9/movie.mp4','199')

def test_signed_64_overflow_is_never_truncated():
    from types import SimpleNamespace
    import stat
    module=primitive()
    facts=SimpleNamespace(st_dev=1,st_ino=2**63,st_uid=0,st_gid=0,st_mode=stat.S_IFREG|0o600,st_nlink=1,st_size=1,st_mtime_ns=1,st_ctime_ns=1)
    with pytest.raises(module.ManifestUnavailable):module._facts('owned',facts)

def test_deep_existing_path_is_refused_before_any_open(media,monkeypatch):
    module=primitive();parts=['deep']*33
    directory=media.joinpath(*parts);directory.mkdir(parents=True)
    (directory/'movie.mp4').write_bytes(b'owned deep movie')
    calls=[];original=module.os.open
    def observe(*args,**kwargs):calls.append(1);return original(*args,**kwargs)
    monkeypatch.setattr(module.os,'open',observe)
    with pytest.raises(module.ManifestUnavailable):module.capture_manifest(media,'/'.join(parts+['movie.mp4']),'199')
    assert calls==[]
