"""Fixed Linux JPEG-to-MP4 worker. No database, user argv or pathname outputs."""
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.media_process_capability import identity,validate
from core.timelapse_manifest import MAX_OBJECTS

MAX_MANIFEST_BYTES=8*1024*1024
FILE_KEYS=('device','inode','uid','gid','mode','link_count','byte_count','mtime_ns','ctime_ns','digest_sha256')


def read_message(fd,deadline,limit):
    data=bytearray()
    while len(data)<=limit:
        if not select.select([fd],[],[],max(0,deadline-time.monotonic()))[0]:raise ValueError('Media deadline')
        chunk=os.read(fd,min(65536,limit+1-len(data)))
        if not chunk:raise ValueError('Incomplete media manifest')
        data.extend(chunk)
        if len(data)>limit:raise ValueError('Media manifest limit')
        if data.endswith(b'\n'):
            return json.loads(data)
        if time.monotonic()>=deadline:raise ValueError('Media deadline')
    raise ValueError('Media manifest limit')


def checked_manifest(value):
    if type(value) is not dict or set(value)!= {'operation_id','recipe_version','encode_fps','frames'}:
        raise ValueError('Media manifest shape')
    operation=value['operation_id']
    if type(operation) is not str or len(operation)!=32 or any(c not in '0123456789abcdef' for c in operation):raise ValueError('Media operation')
    if type(value['recipe_version']) is not int or value['recipe_version']!=1 or type(value['encode_fps']) is not int or value['encode_fps']!=30:raise ValueError('Media recipe')
    frames=value['frames']
    if type(frames) is not list or not 2<=len(frames)<=MAX_OBJECTS:raise ValueError('Media frame count')
    previous=0;total=0
    for frame in frames:
        if type(frame) is not dict or set(frame)!= {'number','name','facts'}:raise ValueError('Media frame shape')
        number=frame['number'];facts=frame['facts']
        if type(number) is not int or not previous<number<(1<<63) or frame['name']!='frame_'+str(number).zfill(6)+'.jpg':raise ValueError('Media frame order')
        previous=number
        if type(facts) is not dict or set(facts)!=set(FILE_KEYS):raise ValueError('Media source facts')
        if any(type(facts[key]) is not int or facts[key]<0 for key in FILE_KEYS[:-1]) or facts['inode']<=0 or facts['link_count']!=1 or facts['mode']!=0o100600 or not 1<=facts['byte_count']<=16*1024*1024:raise ValueError('Media source identity')
        digest=facts['digest_sha256']
        if type(digest) is not str or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):raise ValueError('Media source digest')
        total+=facts['byte_count']
    if total>32*1024**3:raise ValueError('Media source limit')
    return value


def file_facts(fd,deadline=None):
    value=os.fstat(fd);result=identity(fd)
    result.update(mtime_ns=value.st_mtime_ns,ctime_ns=value.st_ctime_ns)
    digest=hashlib.sha256();offset=0
    while True:
        if deadline is not None and time.monotonic()>=deadline:raise ValueError("Media digest deadline")
        block=os.pread(fd,65536,offset)
        if not block:break
        digest.update(block);offset+=len(block)
        if offset>32*1024**3:raise ValueError('Media file limit')
    result['digest_sha256']=digest.hexdigest()
    return result


def read_frame(directory,frame,owner,deadline=None):
    fd=os.open(frame['name'],os.O_RDONLY|os.O_NOFOLLOW,dir_fd=directory)
    try:
        if file_facts(fd,deadline)!=frame['facts'] or any(frame['facts'][key]!=owner[key] for key in ('device','uid','gid')):raise ValueError('Media source changed')
        data=os.pread(fd,frame['facts']['byte_count']+1,0)
        if len(data)!=frame['facts']['byte_count'] or hashlib.sha256(data).hexdigest()!=frame['facts']['digest_sha256'] or file_facts(fd,deadline)!=frame['facts']:raise ValueError('Media source changed')
        return data
    finally:os.close(fd)


def write_bounded(fd,data,deadline):
    view=memoryview(data)
    while view:
        if time.monotonic()>=deadline or not select.select([],[fd],[],max(0,deadline-time.monotonic()))[1]:raise ValueError('Media write deadline')
        try:count=os.write(fd,view[:65536])
        except BlockingIOError:continue
        if count<=0:raise ValueError('Media write failed')
        view=view[count:]



def bounded_probe(command,*,pass_fds,deadline):
    """Cap each probe stream while reading, not after communicate allocation.

    Direct-child stop/reap is local cleanup only. Parent owned-group retirement
    remains mandatory; this helper never establishes descendant quiescence.
    """
    process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,pass_fds=pass_fds)
    streams=(process.stdout,process.stderr)
    output=bytearray();counts={stream.fileno():0 for stream in streams}
    pending=set(counts)
    try:
        for fd in pending:os.set_blocking(fd,False)
        while pending:
            remaining=deadline-time.monotonic()
            if remaining<=0:raise ValueError('Media probe deadline')
            readable,_,_=select.select(list(pending),[],[],remaining)
            if not readable:raise ValueError('Media probe deadline')
            for fd in readable:
                try:block=os.read(fd,min(4096,8193-counts[fd]))
                except BlockingIOError:continue
                if not block:
                    pending.remove(fd);continue
                counts[fd]+=len(block)
                if counts[fd]>8192:raise ValueError('Media probe output limit')
                if fd==process.stdout.fileno():output.extend(block)
        if process.wait(timeout=max(.001,deadline-time.monotonic()))!=0:
            raise ValueError('Media output verification failed')
        if time.monotonic()>=deadline:raise ValueError('Media probe deadline')
        return bytes(output)
    finally:
        try:
            if process.poll() is None:
                process.kill();process.wait(timeout=2)
        finally:
            for stream in streams:stream.close()


def run(input_fd,result_fd,guard_fd,namespace_fd,output_fd):
    if sys.platform!='linux':raise ValueError('Unsupported media platform')
    deadline=time.monotonic()+290
    owner=validate(guard_fd,'guard',identity(guard_fd))
    validate(namespace_fd,'namespace',identity(namespace_fd));validate(output_fd,'output',identity(output_fd))
    value=checked_manifest(read_message(input_fd,deadline,MAX_MANIFEST_BYTES))
    # Sources are read once into bounded JPEG buffers immediately before feeding.
    command=['/usr/bin/ffmpeg','-nostdin','-hide_banner','-loglevel','error','-protocol_whitelist','pipe,file',
        '-f','image2pipe','-vcodec','mjpeg','-framerate','30','-i','pipe:0',
        '-an','-c:v','libx264','-preset','fast','-crf','23','-pix_fmt','yuv420p',
        '-f','mp4','-movflags','+faststart','-y','/proc/self/fd/'+str(output_fd)]
    process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,pass_fds=(guard_fd,output_fd))
    try:
        os.set_blocking(process.stdin.fileno(),False)
        for frame in value['frames']:write_bounded(process.stdin.fileno(),read_frame(namespace_fd,frame,owner,deadline),deadline)
        process.stdin.close()
        if process.wait(timeout=max(.001,deadline-time.monotonic()))!=0:raise ValueError('Media codec failed')
        for frame in value['frames']:read_frame(namespace_fd,frame,owner,deadline)
        probe=bounded_probe(['/usr/bin/ffprobe','-v','error','-protocol_whitelist','file','-count_frames',
            '-show_entries','stream=codec_type,codec_name,width,height,nb_read_frames','-of','json',
            '/proc/self/fd/'+str(output_fd)],pass_fds=(guard_fd,output_fd),deadline=deadline)
        streams=json.loads(probe).get('streams')
        if type(streams) is not list or len(streams)!=1:raise ValueError('Media output stream differs')
        stream=streams[0]
        if (stream.get('codec_type')!='video' or stream.get('codec_name')!='h264'
                or int(stream.get('nb_read_frames',0))!=len(value['frames'])
                or any(type(stream.get(key)) is not int or not 1<=stream[key]<=4096 for key in ('width','height'))):raise ValueError('Media decoded output differs')
        decode=subprocess.run(['/usr/bin/ffmpeg','-nostdin','-v','error','-xerror','-protocol_whitelist','file',
            '-i','/proc/self/fd/'+str(output_fd),'-f','null','-'],pass_fds=(guard_fd,output_fd),
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=max(.001,deadline-time.monotonic()))
        if decode.returncode!=0:raise ValueError('Media output decode failed')
        os.fsync(output_fd)
        facts=file_facts(output_fd,deadline)
        if facts['byte_count']<=0:raise ValueError('Media output empty')
        result={'operation_id':value['operation_id'],'recipe_version':1,'encode_fps':30,'frame_count':len(value['frames']),'output':facts}
        os.set_blocking(result_fd,False)
        write_bounded(result_fd,(json.dumps(result,separators=(',',':'))+'\n').encode(),deadline)
    finally:
        if process.poll() is None:
            process.kill();process.wait(timeout=2)
        if not process.stdin.closed:process.stdin.close()


if __name__=='__main__':
    try:
        if len(sys.argv)!=6:raise ValueError('Invalid media descriptors')
        run(*(int(value) for value in sys.argv[1:]))
    except Exception:
        sys.exit(1)
