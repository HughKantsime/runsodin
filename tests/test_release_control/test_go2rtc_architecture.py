"""An arm64 image must ship an arm64 go2rtc. The real Dockerfile step runs with stub downloads."""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
DOCKERFILE = (ROOT / 'Dockerfile').read_text()
X86_64 = 0x3e
AARCH64 = 0xb7


def _install_step(target: Path) -> str:
    start = DOCKERFILE.index('RUN go2rtc_arch="$(dpkg --print-architecture)"') + len('RUN ')
    end = DOCKERFILE.index('chmod +x /usr/local/bin/go2rtc', start) + len('chmod +x /usr/local/bin/go2rtc')
    return DOCKERFILE[start:end].replace('\\\n', ' ').replace('/usr/local/bin/go2rtc', str(target))


def _run(tmp_path, arch, machine, **env):
    stubs = tmp_path / 'stubs'
    stubs.mkdir()
    target = tmp_path / 'go2rtc'
    downloads = tmp_path / 'downloads'
    # An ELF header says its machine at byte 18, little-endian on both architectures.
    header = b'\x7fELF\x02\x01\x01' + bytes(11) + machine.to_bytes(2, 'little') + bytes(8)
    (tmp_path / 'fixture').write_bytes(header)
    (stubs / 'dpkg').write_text(f'#!/bin/sh\necho "{arch}"\n')
    (stubs / 'curl').write_text(f'#!/bin/sh\necho "$@" >> {downloads}\ncp {tmp_path}/fixture {target}\n')
    # The hashes are for the real releases; the real build checks those.
    (stubs / 'sha256sum').write_text('#!/bin/sh\ncat >/dev/null\n')
    for stub in stubs.iterdir():
        stub.chmod(0o755)
    # The Dockerfile's SHELL line runs each step this way.
    result = subprocess.run(['/bin/bash', '-o', 'pipefail', '-c', _install_step(target)],
                            env={'PATH': f'{stubs}:/usr/bin:/bin', 'GO2RTC_VERSION': '1.9.4', **env},
                            capture_output=True, text=True)
    return result, target, downloads


@pytest.mark.parametrize('arch,machine', [('amd64', X86_64), ('arm64', AARCH64)])
def test_each_architecture_installs_its_own_binary(tmp_path, arch, machine):
    result, target, downloads = _run(tmp_path, arch, machine)
    assert result.returncode == 0, result.stderr
    assert f'go2rtc_linux_{arch}' in downloads.read_text()
    assert target.stat().st_mode & 0o100


@pytest.mark.parametrize('arch,machine', [('amd64', AARCH64), ('arm64', X86_64)])
def test_binary_for_the_wrong_machine_stops_the_build(tmp_path, arch, machine):
    result, target, _ = _run(tmp_path, arch, machine)
    assert result.returncode != 0
    assert not target.stat().st_mode & 0o100


@pytest.mark.parametrize('arch', ['s390x', ''])
def test_unknown_architecture_stops_the_build_before_downloading(tmp_path, arch):
    result, target, downloads = _run(tmp_path, arch, X86_64)
    assert result.returncode != 0 and 'no go2rtc checksum' in result.stderr
    assert not target.exists() and not downloads.exists()


def test_a_build_argument_cannot_give_an_arm64_image_the_amd64_binary(tmp_path):
    """The released bug: `ARG TARGETARCH=amd64` chose the binary whatever the image was."""
    result, _, downloads = _run(tmp_path, 'arm64', AARCH64, TARGETARCH='amd64')
    assert result.returncode == 0, result.stderr
    assert 'go2rtc_linux_arm64' in downloads.read_text() and 'amd64' not in downloads.read_text()
    assert 'TARGETARCH' not in DOCKERFILE


def test_each_architecture_keeps_its_published_hash():
    step = _install_step(Path('/x'))
    assert 'amd64) go2rtc_sha256=8d86510e64e0deadee40d550d46323ddec9f62e14cec56de3a9df0f2f7fe9ada; go2rtc_elf_machine=3e00' in step
    assert 'arm64) go2rtc_sha256=ebea4cf3a0bc3a12190ebba1f2c4b3c8cf4aac099fadb4cd50a669429b074f1c; go2rtc_elf_machine=b700' in step
    assert 'sha256sum -c -' in step
