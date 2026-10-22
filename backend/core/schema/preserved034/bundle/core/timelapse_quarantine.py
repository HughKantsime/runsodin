"""Protected root-local directory primitive. Caller-held media guard required."""
from contextlib import contextmanager
import os
import stat
from core.timelapse_manifest import ObjectIdentity,_root,_facts,_stable


class QuarantineDirectoryUnavailable(RuntimeError):
    pass


def _refuse():
    raise QuarantineDirectoryUnavailable('Quarantine directory could not be confirmed') from None


def _directory(value,root):
    if value.kind!='directory' or stat.S_IMODE(value.mode)!=0o700 or (value.device,value.uid,value.gid)!=(root.device,root.uid,root.gid):_refuse()


@contextmanager
def quarantine_directory(root,expected_root,expected_quarantine=None,*,create=False):
    """Never change unsafe permissions, replace entries, or remove contents.

    Creation is only a primitive for an unbound operation. An existing binding
    must supply its original identity and can never authorize recreation.
    """
    descriptor=None;body_error=None
    try:
        if type(create) is not bool or not isinstance(expected_root,ObjectIdentity) or expected_root.kind!='directory' or expected_root.relative_path!='':_refuse()
        if expected_quarantine is not None:
            if create or not isinstance(expected_quarantine,ObjectIdentity) or expected_quarantine.kind!='directory' or expected_quarantine.relative_path!='.retirement':_refuse()
        with _root(root) as (root_fd,root_identity):
            if _stable(root_identity)!=_stable(expected_root):_refuse()
            if create:
                try:os.mkdir('.retirement',0o700,dir_fd=root_fd)
                except FileExistsError:pass
            before=_facts('.retirement',os.stat('.retirement',dir_fd=root_fd,follow_symlinks=False))
            _directory(before,root_identity)
            if expected_quarantine is not None and _stable(before)!=_stable(expected_quarantine):_refuse()
            descriptor=os.open('.retirement',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=root_fd)
            captured=_facts('.retirement',os.fstat(descriptor))
            if _stable(captured)!=_stable(before):_refuse()
            if create:
                os.fsync(descriptor);os.fsync(root_fd)
            if _stable(_facts('.retirement',os.stat('.retirement',dir_fd=root_fd,follow_symlinks=False)))!=_stable(captured):_refuse()
            try:yield descriptor,captured
            except BaseException as error:
                body_error=error
                raise
            if _stable(_facts('.retirement',os.fstat(descriptor)))!=_stable(captured) or _stable(_facts('.retirement',os.stat('.retirement',dir_fd=root_fd,follow_symlinks=False)))!=_stable(captured):_refuse()
    except QuarantineDirectoryUnavailable:raise
    except Exception as error:
        if error is body_error:raise
        _refuse()
    finally:
        if descriptor is not None:
            closing,descriptor=descriptor,None
            try:os.close(closing)
            except OSError:_refuse()
