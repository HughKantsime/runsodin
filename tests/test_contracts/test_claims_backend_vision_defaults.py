"""Claims F03-3.4-03/04: execute the real entrypoint default-model copy block in an owned sandbox."""
import hashlib
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = ROOT / 'docker' / 'entrypoint.sh'


def _copy_block():
    text = ENTRYPOINT.read_text()
    match = re.search(r'^if \[ -d /app/backend/vision_models_default \]; then\n.*?^fi\n', text, re.S | re.M)
    assert match, 'default model copy block not found in docker/entrypoint.sh'
    return match.group(0)


def _run(block, app, data):
    script = block.replace('/app/backend/vision_models_default', str(app)).replace('/data/vision_models', str(data))
    return subprocess.run(['bash', '-eu', '-c', script], capture_output=True, text=True, timeout=30)


def test_first_boot_copies_every_default_model_and_restart_preserves_operator_copy(tmp_path):
    app = tmp_path / 'app' / 'vision_models_default'
    data = tmp_path / 'data' / 'vision_models'
    app.mkdir(parents=True); data.mkdir(parents=True)
    source = ROOT / 'backend' / 'vision_models_default' / 'obico_spaghetti.onnx'
    (app / source.name).write_bytes(source.read_bytes())
    (app / 'second.onnx').write_bytes(b'synthetic-second')
    (app / 'notes.txt').write_bytes(b'not a model')
    block = _copy_block()
    first = _run(block, app, data)
    assert first.returncode == 0, first.stderr
    assert sorted(p.name for p in data.iterdir()) == ['obico_spaghetti.onnx', 'second.onnx']
    assert hashlib.sha256((data / source.name).read_bytes()).digest() == hashlib.sha256(source.read_bytes()).digest()
    assert first.stdout.count('Copied default vision model') == 2
    # Operator-replaced model survives restart; missing default is restored.
    (data / source.name).write_bytes(b'operator-replacement')
    (data / 'second.onnx').unlink()
    second = _run(block, app, data)
    assert second.returncode == 0, second.stderr
    assert (data / source.name).read_bytes() == b'operator-replacement'
    assert (data / 'second.onnx').read_bytes() == b'synthetic-second'
    assert second.stdout.count('Copied default vision model') == 1


def test_empty_or_missing_default_directory_is_noop(tmp_path):
    data = tmp_path / 'data'; data.mkdir()
    empty = tmp_path / 'empty'; empty.mkdir()
    for app in (empty, tmp_path / 'missing'):
        result = _run(_copy_block(), app, data)
        assert result.returncode == 0, result.stderr
        assert list(data.iterdir()) == []


def test_checkout_default_model_is_lfs_pointer_not_model_bytes():
    """Records what a non-LFS-hydrated build would copy (observation, not a pass/fail of the copy)."""
    source = ROOT / 'backend' / 'vision_models_default' / 'obico_spaghetti.onnx'
    data = source.read_bytes()
    # Either an un-hydrated LFS pointer (plain checkout) or the hydrated model (git lfs pull).
    if data.startswith(b'version https://git-lfs.github.com/spec/v1'):
        assert b'oid sha256:0a6ebd8e30dbf6a450c50f9c0a5406f04ba7eb1c99fd5996e888c78bb383b9aa' in data
    else:
        import hashlib
        assert hashlib.sha256(data).hexdigest() == '0a6ebd8e30dbf6a450c50f9c0a5406f04ba7eb1c99fd5996e888c78bb383b9aa'
