"""Fixed readonly CFR30 probe within the caller's owned process group."""
import json,os,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.timelapse_derivative_worker import probe,collect,command,MAX_BYTES
from core.timelapse_media_worker import file_facts,read_message,write_bounded,FILE_KEYS

def checked_manifest(value):
    if type(value) is not dict or set(value)!={'operation_id','source','deadline'}:raise ValueError('Readonly probe manifest shape')
    identifier=value['operation_id']
    if type(identifier) is not str or len(identifier)!=32 or any(c not in '0123456789abcdef' for c in identifier):raise ValueError('Readonly probe operation')
    source=value['source']
    if type(source) is not dict or set(source)!=set(FILE_KEYS) or any(type(source[k]) is not int or source[k]<0 for k in FILE_KEYS[:-1]) or source['inode']<=0 or source['mode']!=0o100600 or source['link_count']!=1 or not 0<source['byte_count']<=MAX_BYTES:
        raise ValueError('Readonly probe source facts')
    digest=source['digest_sha256']
    if type(digest) is not str or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):raise ValueError('Readonly probe source digest')
    if type(value['deadline']) not in (int,float) or not 0<value['deadline']-time.monotonic()<=300:raise ValueError('Readonly probe deadline')
    return value

def run(input_fd,result_fd,guard_fd,source_fd):
    if sys.platform!='linux':raise ValueError('Unsupported readonly probe platform')
    from core.media_derivative_capability import validate
    validate(guard_fd,'guard');validate(source_fd,'source')
    value=checked_manifest(read_message(input_fd,time.monotonic()+10,8192));deadline=value['deadline']
    if file_facts(source_fd,deadline)!=value['source']:raise ValueError('Readonly probe source changed')
    metadata=probe(source_fd,guard_fd,deadline)
    collect(command('ffmpeg',['-v','error','-xerror','-threads','2','-filter_threads','2','-protocol_whitelist','file','-f','mov','-enable_drefs','0','-use_absolute_path','0','-i',
        '/proc/self/fd/'+str(source_fd),'-f','null','-']),fds=(guard_fd,source_fd),deadline=deadline,maximum=0)
    if file_facts(source_fd,deadline)!=value['source']:raise ValueError('Readonly probe source changed')
    result={'operation_id':value['operation_id'],'source':value['source'],'metadata':metadata}
    os.set_blocking(result_fd,False);write_bounded(result_fd,(json.dumps(result,separators=(',',':'))+'\n').encode(),deadline)

if __name__=='__main__':
    try:
        if len(sys.argv)!=5:raise ValueError('Invalid readonly descriptors')
        run(*(int(value) for value in sys.argv[1:]))
    except Exception:sys.exit(1)
