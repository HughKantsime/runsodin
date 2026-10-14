"""Fixed CFR30 MP4 derivative worker; all media is descriptor-bound."""
import hashlib
import json
import os
from pathlib import Path
import resource
import select
import subprocess
import sys
import time
from fractions import Fraction
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.timelapse_recipes import Recipe, RECIPE_COLUMNS
from core.timelapse_derivative_timing import frame_plan,validate_timestamps,MAX_FRAMES,FPS
from core.timelapse_media_worker import file_facts,read_message,write_bounded,FILE_KEYS

MAX_BYTES=32*1024**3

def limits():
    for kind,value in ((resource.RLIMIT_FSIZE,MAX_BYTES),(resource.RLIMIT_AS,1024**3),
                       (resource.RLIMIT_CPU,240),(resource.RLIMIT_NOFILE,64),(resource.RLIMIT_CORE,0)):
        resource.setrlimit(kind,(value,value))

def command(binary,args):
    return ['/usr/bin/'+binary,'-nostdin',*args] if binary=='ffmpeg' else ['/usr/bin/'+binary,*args]

def launch(argv,*,fds,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE):
    # Worker is already within an owned group. Do not create another session.
    return subprocess.Popen(argv,stdin=stdin,stdout=stdout,stderr=subprocess.DEVNULL,
                            pass_fds=fds,preexec_fn=limits)

def stop(process):
    if process.poll() is None:
        process.kill();process.wait(timeout=2)
    if process.stdout is not None:process.stdout.close()
    if process.stdin is not None:process.stdin.close()

def collect(argv,*,fds,deadline,maximum):
    process=launch(argv,fds=fds);result=bytearray()
    try:
        os.set_blocking(process.stdout.fileno(),False)
        while True:
            remaining=deadline-time.monotonic()
            if remaining<=0 or not select.select([process.stdout],[],[],remaining)[0]:raise ValueError('Derivative deadline')
            block=os.read(process.stdout.fileno(),min(65536,maximum+1-len(result)))
            if not block:break
            result.extend(block)
            if len(result)>maximum:raise ValueError('Derivative probe limit')
        if process.wait(timeout=max(.001,deadline-time.monotonic()))!=0:raise ValueError('Derivative probe failed')
        if time.monotonic()>=deadline:raise ValueError('Derivative deadline')
        return bytes(result)
    finally:stop(process)

def probe(fd,guard,deadline):
    path='/proc/self/fd/'+str(fd)
    value=json.loads(collect(command('ffprobe',['-v','error','-protocol_whitelist','file','-f','mov','-enable_drefs','0','-use_absolute_path','0','-show_streams',
        '-show_entries','stream=codec_type,codec_name,width,height,time_base,avg_frame_rate,r_frame_rate,field_order:stream_tags=rotate:stream_side_data=rotation',
        '-of','json',path]),fds=(fd,guard),deadline=deadline,maximum=8192))
    streams=value.get('streams')
    if type(streams) is not list or len(streams)!=1:raise ValueError('Derivative requires one video stream')
    stream=streams[0]
    if (stream.get('codec_name')!='h264' or stream.get('codec_type')!='video' or
        stream.get('field_order') not in ('progressive','unknown') or stream.get('tags',{}).get('rotate') is not None or
        stream.get('side_data_list') or any(type(stream.get(k)) is not int or not 2<=stream[k]<=4096 or stream[k]%2 for k in ('width','height')) or
        Fraction(stream.get('avg_frame_rate','0'))!=30 or Fraction(stream.get('r_frame_rate','0'))!=30):
        raise ValueError('Unsupported derivative stream')
    data=json.loads(collect(command('ffprobe',['-v','error','-protocol_whitelist','file','-f','mov','-enable_drefs','0','-use_absolute_path','0','-show_frames',
        '-show_entries','frame=best_effort_timestamp,pkt_duration,duration,interlaced_frame','-of','json',path]),
        fds=(fd,guard),deadline=deadline,maximum=8*1024*1024))
    frames=data.get('frames')
    if type(frames) is not list or not 1<=len(frames)<=MAX_FRAMES:raise ValueError('Derivative frame limit')
    stamps=[];base=Fraction(stream['time_base'])
    for frame in frames:
        if type(frame) is not dict or frame.get('interlaced_frame')!=0:raise ValueError('Unsupported derivative frame')
        stamp=frame.get('best_effort_timestamp');duration=frame.get('duration',frame.get('pkt_duration'))
        if type(stamp) is not int or type(duration) is not int or duration*base!=Fraction(1,FPS):raise ValueError('Unsupported derivative timestamp')
        stamps.append(stamp)
    count=validate_timestamps(stamps,stream['time_base'])
    return dict(count=count,width=stream['width'],height=stream['height'])

def checked_manifest(value):
    if type(value) is not dict or set(value)!={'operation_id','kind','recipe','source','deadline'}:raise ValueError('Derivative manifest shape')
    identifier=value['operation_id']
    if type(identifier) is not str or len(identifier)!=32 or any(c not in '0123456789abcdef' for c in identifier):raise ValueError('Derivative operation')
    if value['kind'] not in ('trim','speed') or type(value['recipe']) is not dict or set(value['recipe'])!=set(RECIPE_COLUMNS):raise ValueError('Derivative recipe shape')
    recipe=Recipe(kind=value['kind'],**value['recipe'])
    source=value['source']
    if type(source) is not dict or set(source)!=set(FILE_KEYS):raise ValueError('Derivative source shape')
    if any(type(source[k]) is not int or source[k]<0 for k in FILE_KEYS[:-1]) or source['inode']<=0 or source['link_count']!=1 or source['mode']!=0o100600 or not 1<=source['byte_count']<=MAX_BYTES:raise ValueError('Derivative source identity')
    digest=source['digest_sha256']
    if type(digest) is not str or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):raise ValueError('Derivative source digest')
    deadline=value['deadline']
    if type(deadline) not in (int,float) or not 0<deadline-time.monotonic()<=300:raise ValueError('Derivative deadline')
    return recipe

def transform(source,output,guard,metadata,indices,deadline):
    size=metadata['width']*metadata['height']*3//2
    decoder=launch(command('ffmpeg',['-v','error','-xerror','-threads','2','-filter_threads','2','-protocol_whitelist','file',
        '-f','mov','-enable_drefs','0','-use_absolute_path','0','-i','/proc/self/fd/'+str(source),'-map','0:v:0','-an','-pix_fmt','yuv420p','-f','rawvideo','-']),fds=(source,guard))
    encoder=None
    try:
        encoder=launch(command('ffmpeg',['-v','error','-threads','2','-filter_threads','2','-f','rawvideo','-pixel_format','yuv420p',
            '-video_size',str(metadata['width'])+'x'+str(metadata['height']),'-framerate','30','-i','pipe:0',
            '-an','-c:v','libx264','-preset','fast','-crf','23','-pix_fmt','yuv420p','-f','mp4','-movflags','+faststart',
            '-y','/proc/self/fd/'+str(output)]),fds=(output,guard),stdin=subprocess.PIPE,stdout=subprocess.DEVNULL)
        os.set_blocking(decoder.stdout.fileno(),False);os.set_blocking(encoder.stdin.fileno(),False)
        j=0
        for i in range(metadata['count']):
            frame=bytearray()
            while len(frame)<size:
                remaining=deadline-time.monotonic()
                if remaining<=0 or not select.select([decoder.stdout],[],[],remaining)[0]:raise ValueError('Derivative decode deadline')
                part=os.read(decoder.stdout.fileno(),min(65536,size-len(frame)))
                if not part:raise ValueError('Derivative source truncated')
                frame.extend(part)
            while j<len(indices) and indices[j]==i:
                write_bounded(encoder.stdin.fileno(),frame,deadline);j+=1
            if os.fstat(output).st_size>MAX_BYTES:raise ValueError('Derivative output size')
        remaining=deadline-time.monotonic()
        if remaining<=0 or not select.select([decoder.stdout],[],[],remaining)[0] or os.read(decoder.stdout.fileno(),1):raise ValueError('Derivative source frame count differs')
        if j!=len(indices) or decoder.wait(timeout=max(.001,deadline-time.monotonic()))!=0:raise ValueError('Derivative source decode failed')
        encoder.stdin.close()
        if encoder.wait(timeout=max(.001,deadline-time.monotonic()))!=0:raise ValueError('Derivative encode failed')
    finally:
        stop(decoder)
        if encoder is not None:stop(encoder)

def run(input_fd,result_fd,guard_fd,source_fd,output_fd):
    if sys.platform!='linux':raise ValueError('Unsupported derivative platform')
    from core.media_derivative_capability import validate
    validate(guard_fd,'guard');validate(source_fd,'source');validate(output_fd,'output')
    value=read_message(input_fd,time.monotonic()+10,8192);recipe=checked_manifest(value);deadline=value['deadline']
    if file_facts(source_fd,deadline)!=value['source']:raise ValueError('Derivative source changed')
    metadata=probe(source_fd,guard_fd,deadline);indices=frame_plan(recipe,metadata['count'])
    transform(source_fd,output_fd,guard_fd,metadata,indices,deadline)
    output=probe(output_fd,guard_fd,deadline)
    if output!=dict(metadata,count=len(indices)):raise ValueError('Derivative decoded output differs')
    collect(command('ffmpeg',['-v','error','-xerror','-threads','2','-filter_threads','2','-protocol_whitelist','file','-f','mov','-enable_drefs','0','-use_absolute_path','0','-i',
        '/proc/self/fd/'+str(output_fd),'-f','null','-']),fds=(guard_fd,output_fd),deadline=deadline,maximum=0)
    if file_facts(source_fd,deadline)!=value['source']:raise ValueError('Derivative source changed')
    os.fsync(output_fd);facts=file_facts(output_fd,deadline)
    if not 0<facts['byte_count']<=MAX_BYTES:raise ValueError('Derivative output limit')
    result={'operation_id':value['operation_id'],'recipe':value['recipe'],'kind':value['kind'],
        'frame_count':len(indices),'source_frame_count':metadata['count'],'output':facts}
    os.set_blocking(result_fd,False);write_bounded(result_fd,(json.dumps(result,separators=(',',':'))+'\n').encode(),deadline)

if __name__=='__main__':
    try:
        if len(sys.argv)!=6:raise ValueError('Invalid derivative descriptors')
        run(*(int(value) for value in sys.argv[1:]))
    except Exception:sys.exit(1)
