"""Actual birth evidence from a caller-owned FD; unsupported is never ctime."""
from dataclasses import dataclass
import ctypes
import os
import platform
import stat
import struct
import sys


class BirthUnavailable(RuntimeError):
    """Caller must keep root recovery read-only without supported evidence."""


@dataclass(frozen=True)
class FDBirth:
    seconds: int
    nanoseconds: int
    device: int
    inode: int
    kind: str
    facility: str

    @property
    def timestamp_ns(self):
        return self.seconds * 1_000_000_000 + self.nanoseconds


def _unavailable():
    raise BirthUnavailable('FD birth evidence unavailable') from None


def _finite_birth(seconds, nanoseconds):
    if (type(seconds) is not int or type(nanoseconds) is not int
            or seconds <= 0 or not 0 <= nanoseconds < 1_000_000_000
            or seconds * 1_000_000_000 + nanoseconds > 2**63 - 1):
        _unavailable()
    return seconds, nanoseconds


def _load_statx():
    # The cached native proof qualifies this 64-bit Linux UAPI layout. Other
    # architectures/libcs require their own proof, not a guessed syscall ABI.
    if (platform.machine().lower() not in ('x86_64', 'amd64')
            or ctypes.sizeof(ctypes.c_void_p) != 8
            or ctypes.sizeof(ctypes.c_uint) != 4
            or ctypes.sizeof(ctypes.c_longlong) != 8):
        _unavailable()
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        version = libc.gnu_get_libc_version
        version.restype = ctypes.c_char_p
        parts = version().decode('ascii').split('.')
        if len(parts) != 2 or not all(p.isdigit() for p in parts) or int(parts[0]) != 2 or int(parts[1]) < 28:
            _unavailable()
        function = libc.statx
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                             ctypes.c_uint, ctypes.c_void_p]
        function.restype = ctypes.c_int
        return function
    except BirthUnavailable:
        raise
    except Exception:
        _unavailable()


def _linux_birth(fd, before):
    function = _load_statx()
    buffer = ctypes.create_string_buffer(256)
    try:
        # AT_EMPTY_PATH binds to this FD; no pathname traversal or follow.
        if function(fd, b'', 0x1000 | 0x100, 0x7ff | 0x800, buffer) != 0:
            _unavailable()
        data = buffer.raw
        mask = struct.unpack_from('=I', data, 0)[0]
        inode = struct.unpack_from('=Q', data, 32)[0]
        mode = struct.unpack_from('=H', data, 28)[0]
        major, minor = struct.unpack_from('=II', data, 136)
        if (mask & (0x800 | 0x100 | 0x1) != (0x800 | 0x100 | 0x1)
                or inode != before.st_ino or stat.S_IFMT(mode) != stat.S_IFMT(before.st_mode)
                or major != os.major(before.st_dev) or minor != os.minor(before.st_dev)):
            _unavailable()
        seconds, nanoseconds = struct.unpack_from('=qI', data, 80)
        return _finite_birth(seconds, nanoseconds)
    except BirthUnavailable:
        raise
    except Exception:
        _unavailable()


def read_fd_birth(fd):
    """Return exact positive birth tuple without opening, closing or owning FD.

    This proves this observed descriptor's reported birth, not permanent inode
    uniqueness, named-path identity, root enrollment or permission to delete.
    """
    if type(fd) is not int or not 0 <= fd <= 2**31 - 1:
        _unavailable()
    try:
        before = os.fstat(fd)
        if stat.S_ISREG(before.st_mode):
            kind = 'regular'
        elif stat.S_ISDIR(before.st_mode):
            kind = 'directory'
        else:
            _unavailable()
        if sys.platform == 'linux':
            seconds, nanoseconds = _linux_birth(fd, before)
            facility = 'linux_statx'
        elif sys.platform == 'darwin':
            birth_ns = getattr(before, 'st_birthtime_ns', None)
            if type(birth_ns) is not int or birth_ns <= 0:
                _unavailable()
            seconds, nanoseconds = _finite_birth(*divmod(birth_ns, 1_000_000_000))
            facility = 'macos_birthtime_ns'
        else:
            _unavailable()
        after = os.fstat(fd)
        if ((after.st_dev, after.st_ino, stat.S_IFMT(after.st_mode))
                != (before.st_dev, before.st_ino, stat.S_IFMT(before.st_mode))):
            _unavailable()
        if type(before.st_dev) is not int or type(before.st_ino) is not int or before.st_dev < 0 or before.st_ino <= 0:
            _unavailable()
        return FDBirth(seconds, nanoseconds, before.st_dev, before.st_ino, kind, facility)
    except BirthUnavailable:
        raise
    except Exception:
        _unavailable()
