"""Root startup creator. Stdlib only, launched with absolute Python -I -S."""
import json
import os
import stat
import uuid

DIRECTORY='/odin-runtime'


def clock_domain():
    with open('/proc/sys/kernel/random/boot_id','r',encoding='ascii') as source:
        boot=source.read(65).strip()
    if str(uuid.UUID(boot))!=boot:
        raise ValueError('runtime clock unavailable')
    namespace=os.stat('/proc/self/ns/time')
    return boot,namespace.st_dev,namespace.st_ino


def create_identity():
    if os.geteuid()!=0:
        raise ValueError('trusted startup unavailable')
    try:
        os.mkdir(DIRECTORY,0o755)
        os.chmod(DIRECTORY,0o755)
    except FileExistsError:
        pass
    directory=os.open(DIRECTORY,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    temporary=None
    try:
        info=os.fstat(directory)
        if info.st_uid!=0 or info.st_mode&0o022:
            raise ValueError('trusted runtime directory unavailable')
        try:
            old=os.stat('identity.json',dir_fd=directory,follow_symlinks=False)
        except FileNotFoundError:
            old=None
        if old is not None and (not stat.S_ISREG(old.st_mode) or old.st_uid!=0
                                or old.st_nlink!=1 or stat.S_IMODE(old.st_mode)!=0o444):
            raise ValueError('trusted runtime record unavailable')
        if old is not None:
            # A failed new trusted start must not leave the prior incarnation
            # available to newly spawned workers.
            os.unlink('identity.json',dir_fd=directory)
        boot,device,inode=clock_domain()
        value=dict(version=1,incarnation=uuid.uuid4().hex,boot_uuid=boot,
                   time_namespace_device=device,time_namespace_inode=inode)
        content=(json.dumps(value,separators=(',',':'))+'\n').encode('ascii')
        temporary='.identity-'+uuid.uuid4().hex
        descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,
                           0o600,dir_fd=directory)
        try:
            with os.fdopen(descriptor,'wb') as target:
                target.write(content)
                target.flush()
                os.fchmod(target.fileno(),0o444)
                os.fsync(target.fileno())
                published_identity=os.fstat(target.fileno())
            os.replace(temporary,'identity.json',src_dir_fd=directory,dst_dir_fd=directory)
            temporary=None
            try:
                os.fsync(directory)
            except OSError:
                current=os.stat('identity.json',dir_fd=directory,follow_symlinks=False)
                if (current.st_dev,current.st_ino)==(published_identity.st_dev,published_identity.st_ino):
                    os.unlink('identity.json',dir_fd=directory)
                raise
        finally:
            if temporary is not None:
                os.unlink(temporary,dir_fd=directory)
    finally:
        os.close(directory)


if __name__=='__main__':
    try:
        create_identity()
    except (OSError,ValueError):
        raise SystemExit('Fresh printer evidence runtime unavailable') from None
