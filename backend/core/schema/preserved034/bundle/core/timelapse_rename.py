"""Native descriptor-relative no-overwrite move, not retirement authorization."""
import ctypes
import errno
import os
import stat
import sys

class RenameUnavailable(RuntimeError):
    """No fallback may replace an atomic no-overwrite move."""


def _refuse():
    raise RenameUnavailable('Native no-replace move is unavailable')


def _component(value):
    if type(value) is not str or not value or value in ('.','..') or any(character in value for character in ('/','\\','\x00')):
        _refuse()
    try:
        encoded = os.fsencode(value)
    except UnicodeError:
        _refuse()
    if len(encoded) > 255:_refuse()
    return encoded


def _native():
    if sys.platform == 'darwin':name,flag = 'renameatx_np',4
    elif sys.platform.startswith('linux'):name,flag = 'renameat2',1
    else:_refuse()
    try:
        function = getattr(ctypes.CDLL(None,use_errno=True),name)
    except (AttributeError,OSError):
        _refuse()
    function.argtypes = [ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
    function.restype = ctypes.c_int
    return function,flag


def rename_noreplace(source_dir_fd,source_name,destination_dir_fd,destination_name):
    """Move one entry without overwriting; caller retains both descriptors.

    A successful syscall is not identity, intent or durability proof. The
    caller must hold operation ownership, recheck identity and fsync directories.
    """
    source = _component(source_name)
    destination = _component(destination_name)
    for descriptor in (source_dir_fd,destination_dir_fd):
        if type(descriptor) is not int or not 0 <= descriptor <= (1 << 31)-1:_refuse()
        try:
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):_refuse()
        except OSError:
            _refuse()
    source_directory = os.fstat(source_dir_fd)
    destination_directory = os.fstat(destination_dir_fd)
    if (source_directory.st_dev,source_directory.st_ino) == (destination_directory.st_dev,destination_directory.st_ino) and source == destination:
        _refuse()
    function,flag = _native()
    ctypes.set_errno(0)
    result = function(source_dir_fd,source,destination_dir_fd,destination,flag)
    if result != 0:
        code = ctypes.get_errno()
        if code in (errno.ENOSYS,errno.EINVAL,errno.ENOTSUP,errno.EOPNOTSUPP) or code == 0:
            _refuse()
        raise OSError(code,'Native no-replace move refused')
