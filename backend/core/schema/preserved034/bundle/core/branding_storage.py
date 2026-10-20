"""Shared branding root and descriptor-backed public assets, without redirects."""
import hashlib
import mimetypes
import os
from pathlib import Path
import stat
import threading
import weakref

import anyio
from starlette.responses import Response


def branding_directory(default):
    configured=os.environ.get('ODIN_BRANDING_DIRECTORY')
    directory=Path(default) if configured is None else Path(configured)
    if not directory.is_absolute() or '..' in directory.parts:
        raise ValueError('Branding storage configuration unavailable')
    return directory


def _close(fds):
    for descriptor in reversed(fds):
        try:
            os.close(descriptor)
        except OSError:
            pass  # Attempt every owned close; do not retry a possibly reused FD.


def _open_chain(directory,*,create=False):
    flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
    fds=[os.open('/',flags)]
    try:
        for part in directory.parts[1:]:
            try:
                descriptor=os.open(part,flags,dir_fd=fds[-1])
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part,0o755,dir_fd=fds[-1])
                except FileExistsError:
                    pass
                descriptor=os.open(part,flags,dir_fd=fds[-1])
            fds.append(descriptor)
        return fds
    except BaseException:
        _close(fds)
        raise


class BrandingAssets:
    """Pin all parent directories; read bounded leaf bytes through owned FDs.

    No FileResponse pathname reopening after validation and no broad symlink
    following. Holding directory descriptors also prevents inode reuse revival.
    """
    def __init__(self,directory):
        self.directory=Path(directory)
        self._fds=_open_chain(self.directory,create=True)
        self._identities=tuple((os.fstat(fd).st_dev,os.fstat(fd).st_ino) for fd in self._fds)
        self._lock=threading.RLock()
        self._cleanup=weakref.finalize(self,_close,self._fds)

    def close(self):
        with self._lock:
            self._cleanup()

    def _read(self,filename):
        if (not filename or filename in ('.','..') or len(filename.encode('utf-8'))>255
                or any(c in filename for c in ('/','\\','\x00'))):
            return None
        with self._lock:
            if not self._cleanup.alive:
                return None
            current=[]
            descriptor=None
            try:
                current=_open_chain(self.directory)
                if tuple((os.fstat(fd).st_dev,os.fstat(fd).st_ino) for fd in current)!=self._identities:
                    return None
                descriptor=os.open(filename,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,
                                   dir_fd=self._fds[-1])
                before=os.fstat(descriptor)
                if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>16*1024*1024:
                    return None
                chunks=[];total=0
                while True:
                    chunk=os.read(descriptor,min(65536,16*1024*1024+1-total))
                    if not chunk:
                        break
                    chunks.append(chunk);total+=len(chunk)
                    if total>16*1024*1024:
                        return None
                after=os.fstat(descriptor)
                if (total!=before.st_size or (before.st_size,before.st_mtime_ns,before.st_ctime_ns)
                        !=(after.st_size,after.st_mtime_ns,after.st_ctime_ns)):
                    return None
                return b''.join(chunks)
            except (OSError,ValueError):
                return None
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                _close(current)

    async def __call__(self,scope,receive,send):
        if scope['type']!='http':
            return
        if scope['method'] not in ('GET','HEAD'):
            await Response(status_code=405,headers={'Allow':'GET, HEAD'})(scope,receive,send)
            return
        path=scope['path'];prefix=scope.get('root_path','')
        if prefix and path.startswith(prefix):
            path=path[len(prefix):]
        filename=path[1:] if path.startswith('/') else path
        content=await anyio.to_thread.run_sync(self._read,filename)
        if content is None:
            await Response(status_code=404)(scope,receive,send)
            return
        etag='"'+hashlib.sha256(content).hexdigest()+'"'
        headers={'ETag':etag,'Cache-Control':'no-cache','X-Content-Type-Options':'nosniff'}
        if any(k.lower()==b'if-none-match' and v.decode('latin1')==etag for k,v in scope['headers']):
            await Response(status_code=304,headers=headers)(scope,receive,send)
            return
        headers['Content-Length']=str(len(content))
        await Response(content if scope['method']=='GET' else b'',headers=headers,
                       media_type=mimetypes.guess_type(filename)[0] or 'application/octet-stream')(scope,receive,send)
