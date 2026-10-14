"""Copy bundled defaults as the application user without replacing user assets."""
import os
import stat
import uuid


def copy_defaults():
    if os.geteuid()!=10001:
        raise ValueError('application identity unavailable')
    flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
    data=os.open('/data',flags)
    static=None
    target=None
    source=None
    try:
        # A fresh /data volume hides the image's directories; create them owned by us.
        for parent,name in ((data,'static'),):
            try:os.mkdir(name,0o755,dir_fd=parent)
            except FileExistsError:pass
        static=os.open('static',flags,dir_fd=data)
        try:os.mkdir('branding',0o755,dir_fd=static)
        except FileExistsError:pass
        target=os.open('branding',flags,dir_fd=static)
        source=os.open('/usr/local/share/odin/branding-defaults',flags)
        original=os.fstat(source)
        if original.st_uid!=0 or original.st_mode&0o022:
            raise ValueError('bundled defaults unavailable')
        for descriptor in (data,static,target):
            item=os.fstat(descriptor)
            # Root ownership is as trustworthy as ours: Docker Desktop bind mounts
            # (macOS/Windows) ignore chown and report root. Shared write is never allowed.
            if item.st_uid not in (0,10001) or item.st_mode&0o022:
                raise ValueError('branding storage unavailable')
        entries=[]
        for name in os.listdir(source):
            item=os.stat(name,dir_fd=source,follow_symlinks=False)
            if (not stat.S_ISREG(item.st_mode) or item.st_uid!=0 or item.st_nlink!=1
                    or item.st_mode&0o022 or item.st_size>16*1024*1024):
                raise ValueError('bundled default unavailable')
            try:
                existing=os.stat(name,dir_fd=target,follow_symlinks=False)
            except FileNotFoundError:
                existing=None
            if existing is not None and (not stat.S_ISREG(existing.st_mode) or existing.st_nlink!=1):
                raise ValueError('branding asset unavailable')
            entries.append((name,existing))
        for name,existing in entries:
            if existing is not None:
                continue
            source_file=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=source)
            destination=None
            temporary='.default-'+uuid.uuid4().hex
            try:
                destination=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,
                                    0o600,dir_fd=target)
                while True:
                    chunk=os.read(source_file,65536)
                    if not chunk:
                        break
                    offset=0
                    while offset<len(chunk):
                        offset+=os.write(destination,chunk[offset:])
                os.fchmod(destination,0o644)
                os.fsync(destination)
                try:
                    os.link(temporary,name,src_dir_fd=target,dst_dir_fd=target,follow_symlinks=False)
                except FileExistsError:
                    pass  # Never replace a concurrent user asset.
            finally:
                os.close(source_file)
                if destination is not None:
                    os.close(destination)
                    os.unlink(temporary,dir_fd=target)
        os.fsync(target)
    finally:
        for descriptor in (source,target,static,data):
            if descriptor is not None:
                os.close(descriptor)


if __name__=='__main__':
    try:
        copy_defaults()
    except (OSError,ValueError):
        raise SystemExit('Branding defaults unavailable') from None
