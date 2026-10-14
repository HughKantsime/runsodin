"""Owned bounded regular-file snapshots with actual descriptor and disk behavior."""
import hashlib,os,stat
from pathlib import Path
import pytest
from core import print_payload_snapshot as module


def test_snapshot_is_private_exact_and_survives_original_replacement(tmp_path):
    source=tmp_path/'original.gcode';payload=b'G90\n';source.write_bytes(payload)
    digest=module.hash_regular_payload(source)
    with module.stage_payload(source,digest) as snapshot:
        staged=Path(snapshot.path)
        assert staged!=source and staged.suffix==source.suffix
        assert staged.read_bytes()==payload and snapshot.sha256==hashlib.sha256(payload).hexdigest()
        assert stat.S_IMODE(staged.stat().st_mode)==0o400
        assert stat.S_IMODE(staged.parent.stat().st_mode)==0o700
        assert str(staged) not in repr(snapshot)
        source.unlink();source.write_bytes(b'changed\n')
        assert staged.read_bytes()==payload
    assert not staged.exists() and not staged.parent.exists()
    assert source.read_bytes()==b'changed\n'


@pytest.mark.parametrize('kind',['empty','directory','symlink','fifo','oversize'])
def test_unsafe_or_oversized_source_refuses_without_staging(tmp_path,kind):
    source=tmp_path/'source.gcode'
    if kind=='empty':source.touch()
    elif kind=='directory':source.mkdir()
    elif kind=='symlink':
        target=tmp_path/'target';target.write_bytes(b'inert');source.symlink_to(target)
    elif kind=='fifo':os.mkfifo(source)
    else:
        with source.open('wb') as output:output.truncate(module.MAX_PAYLOAD_BYTES+1)
    with pytest.raises(module.PayloadUnavailable):module.hash_regular_payload(source)
    with pytest.raises(module.PayloadUnavailable),module.stage_payload(source,'a'*64):
        pytest.fail('Unsafe source yielded an adapter snapshot')


def test_source_changed_before_snapshot_refuses_and_removes_owned_destination(tmp_path,monkeypatch):
    source=tmp_path/'source.gcode';source.write_bytes(b'original')
    digest=module.hash_regular_payload(source);source.write_bytes(b'changed')
    original=module.tempfile.TemporaryDirectory;created=[]
    def temporary(*args,**kwargs):
        value=original(*args,**kwargs);created.append(Path(value.name));return value
    monkeypatch.setattr(module.tempfile,'TemporaryDirectory',temporary)
    with pytest.raises(module.PayloadUnavailable),module.stage_payload(source,digest):pytest.fail('Changed payload admitted')
    assert created and all(not path.exists() for path in created)


def test_snapshot_removed_after_adapter_exception(tmp_path):
    source=tmp_path/'source.bgcode';source.write_bytes(b'inert')
    with pytest.raises(RuntimeError),module.stage_payload(source,module.hash_regular_payload(source)) as snapshot:
        staged=Path(snapshot.path);raise RuntimeError('Synthetic adapter exception')
    assert not staged.exists() and source.exists()


def test_growth_while_copying_refuses(tmp_path,monkeypatch):
    source=tmp_path/'source.gcode';source.write_bytes(b'inert')
    original=module._copy_hash
    observed=[]
    def grow(stream,info,destination=None):
        observed.append(stream.fileno())
        with source.open('ab') as output:output.write(b'changed')
        return original(stream,info,destination)
    monkeypatch.setattr(module,'_copy_hash',grow)
    with pytest.raises(module.PayloadUnavailable),module.stage_payload(source,hashlib.sha256(b'inert').hexdigest()):pytest.fail('Growing source admitted')
    assert len(observed)==1

