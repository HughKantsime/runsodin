"""Bounded daemon-reported readiness, never printer-action authority."""
import json
import os
import stat
import time
import uuid
import hashlib

MAX_STATUS_BYTES = 32768
STATUS_TTL_SECONDS = 90


def registry_fingerprint(model_id, filename, input_size, metadata, file_digest):
    return hashlib.sha256(repr((model_id,filename,input_size or 640,metadata,file_digest)).encode()).hexdigest()


def _directory(path, create=False):
    if create:
        try:os.mkdir(path, 0o700)
        except FileExistsError:pass
    fd=os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    info=os.fstat(fd)
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        os.close(fd)
        raise ValueError('Runtime directory is not privately owned')
    return fd


def write_status(directory, incarnation, generation, models, now=None):
    value={'version':1,'incarnation':incarnation,'generation':generation,
           'reported_at':time.time() if now is None else now,'models':models}
    payload=json.dumps(value,allow_nan=False,separators=(',',':')).encode()
    if len(payload)>MAX_STATUS_BYTES:raise ValueError('Runtime report exceeds limit')
    directory_fd=_directory(directory,create=True)
    name='.'+uuid.uuid4().hex+'.tmp'
    fd=None
    try:
        fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory_fd)
        with os.fdopen(fd,'wb') as stream:
            fd=None
            stream.write(payload);stream.flush();os.fsync(stream.fileno())
        os.replace(name,'status.json',src_dir_fd=directory_fd,dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        if fd is not None:os.close(fd)
        try:os.unlink(name,dir_fd=directory_fd)
        except FileNotFoundError:pass
        os.close(directory_fd)


def read_status(directory, now=None):
    directory_fd=None
    try:
        directory_fd=_directory(directory)
        fd=os.open('status.json',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory_fd)
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            if info.st_size > MAX_STATUS_BYTES or not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
                return None
            value=json.loads(stream.read(MAX_STATUS_BYTES+1))
        if (not isinstance(value,dict) or type(value.get('version')) is not int or value['version'] != 1
            or type(value.get('generation')) is not int or value['generation']<0
            or not isinstance(value.get('incarnation'),str) or len(value['incarnation'])!=32
            or not isinstance(value.get('models'),dict) or len(value['models'])>4):return None
        stamp=value.get('reported_at')
        current=time.time() if now is None else now
        if type(stamp) not in (int,float) or not 0 <= current-stamp <= STATUS_TTL_SECONDS:return None
        for kind, model in value['models'].items():
            if kind not in ('spaghetti','first_layer','detachment','build_plate_empty'):return None
            if (not isinstance(model,dict) or type(model.get('id')) is not int
                or model.get('state') not in ('loaded','ready','unavailable')
                or not isinstance(model.get('fingerprint'),str)
                or len(model['fingerprint']) not in (0,64)):return None
        return value
    except (OSError,ValueError,TypeError,RecursionError):return None
    finally:
        if directory_fd is not None:os.close(directory_fd)


def reported_readiness(report, kind, model_id, fingerprint):
    if report is None:return {'state':'unknown','reported_at':None}
    model=report['models'].get(kind)
    if (not model or model['id']!=model_id or not fingerprint
        or model['fingerprint']!=fingerprint):return {'state':'unknown','reported_at':None}
    return {'state':model['state'],'reported_at':report['reported_at']}
