"""Read root-created supervisor identity; no identity or clock fallback."""
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass


class RuntimeIdentityUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class RuntimeIdentity:
    incarnation:str
    clock_domain:str
    directory_device:int
    directory_inode:int
    directory_ctime_ns:int
    file_device:int
    file_inode:int
    file_ctime_ns:int
    content:bytes


def _clock_domain():
    with open('/proc/sys/kernel/random/boot_id','r',encoding='ascii') as source:
        boot=source.read(65).strip()
    if str(uuid.UUID(boot))!=boot:
        raise ValueError('invalid clock')
    namespace=os.stat('/proc/self/ns/time')
    return boot,namespace.st_dev,namespace.st_ino


def _unique_object(items):
    result={}
    for key,value in items:
        if key in result:
            raise ValueError('duplicate runtime field')
        result[key]=value
    return result


def read_runtime_identity():
    """Fixed trusted path, with bounded regular-file read and pinned identities."""
    try:
        if os.environ.get('ODIN_AMS_RUNTIME_READY') != '1':
            raise ValueError('trusted startup unavailable')
        directory=os.open('/odin-runtime',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            parent=os.fstat(directory)
            if parent.st_uid!=0 or parent.st_mode&0o022:
                raise ValueError('invalid runtime directory')
            descriptor=os.open('identity.json',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,
                               dir_fd=directory)
            try:
                before=os.fstat(descriptor)
                if (not stat.S_ISREG(before.st_mode) or before.st_uid!=0
                    or stat.S_IMODE(before.st_mode)!=0o444 or before.st_nlink!=1
                    or not 0<before.st_size<=512):
                    raise ValueError('invalid runtime file')
                content=os.read(descriptor,513)
                after=os.fstat(descriptor)
                if (len(content)!=before.st_size or len(content)>512
                        or (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)
                        !=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns)):
                    raise ValueError('runtime changed during read')
            finally:
                os.close(descriptor)
            current=os.stat('identity.json',dir_fd=directory,follow_symlinks=False)
            if (current.st_dev,current.st_ino,current.st_ctime_ns)!=(after.st_dev,after.st_ino,after.st_ctime_ns):
                raise ValueError('runtime replaced during read')
            value=json.loads(content,object_pairs_hook=_unique_object)
            if (type(value) is not dict or set(value)!={'version','incarnation','boot_uuid',
                    'time_namespace_device','time_namespace_inode'}
                or type(value['version']) is not int or value['version']!=1
                or type(value['incarnation']) is not str
                or re.fullmatch('[0-9a-f]{32}',value['incarnation']) is None
                or type(value['boot_uuid']) is not str
                or any(type(value[key]) is not int or value[key]<0
                       for key in ('time_namespace_device','time_namespace_inode'))):
                raise ValueError('invalid runtime content')
            clock=_clock_domain()
            if (value['boot_uuid'],value['time_namespace_device'],value['time_namespace_inode'])!=clock:
                raise ValueError('runtime clock changed')
            path=os.stat('/odin-runtime',follow_symlinks=False)
            if (path.st_dev,path.st_ino,path.st_ctime_ns)!=(parent.st_dev,parent.st_ino,parent.st_ctime_ns):
                raise ValueError('runtime directory replaced')
            return RuntimeIdentity(value['incarnation'],'linux-boot-time-v1:%s:%s:%s'%clock,
                parent.st_dev,parent.st_ino,parent.st_ctime_ns,after.st_dev,after.st_ino,after.st_ctime_ns,content)
        finally:
            os.close(directory)
    except (OSError,ValueError,UnicodeError,TypeError) as error:
        raise RuntimeIdentityUnavailable('Fresh printer evidence runtime unavailable') from None


def revalidate_runtime_identity(identity):
    if type(identity) is not RuntimeIdentity or read_runtime_identity()!=identity:
        raise RuntimeIdentityUnavailable('Fresh printer evidence runtime unavailable')
    return identity
