"""Actual root creator and uid10001 reader assertions in a disposable container."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

spec=importlib.util.spec_from_file_location('runtime_reader','/usr/local/libexec/runtime_reader.py')
reader=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=reader
spec.loader.exec_module(reader)
CREATE=[sys.executable,'-I','-S','/usr/local/libexec/odin-runtime-identity.py']


def child(code):
    def drop():
        os.setgroups([])
        os.setgid(10001)
        os.setuid(10001)
    return subprocess.check_output([sys.executable,'-I','-S','-c',code],
        text=True,preexec_fn=drop,timeout=10).strip()


def refuse(call):
    try:
        call()
    except reader.RuntimeIdentityUnavailable as error:
        assert str(error)=='Fresh printer evidence runtime unavailable'
    else:
        raise AssertionError('Untrusted runtime accepted')


def main():
    subprocess.run(CREATE,check=True,timeout=10)
    os.environ['ODIN_AMS_RUNTIME_READY']='1'
    first=reader.read_runtime_identity()
    os.environ['ODIN_AMS_RUNTIME_READY']='0'
    refuse(reader.read_runtime_identity)
    os.environ.pop('ODIN_AMS_RUNTIME_READY')
    refuse(reader.read_runtime_identity)
    os.environ['ODIN_AMS_RUNTIME_READY']='1'
    load="import importlib.util,sys,json; s=importlib.util.spec_from_file_location('r','/usr/local/libexec/runtime_reader.py'); r=importlib.util.module_from_spec(s);sys.modules['r']=r;s.loader.exec_module(r);v=r.read_runtime_identity();print(json.dumps([v.incarnation,v.clock_domain]))"
    one=json.loads(child(load))
    two=json.loads(child(load))
    assert one==two==[first.incarnation,first.clock_domain]
    assert child("try:\n f=open('/odin-runtime/identity.json','a');f.close();print('writable')\nexcept PermissionError:print('refused')")=='refused'
    assert child("import subprocess,sys;v=subprocess.run([sys.executable,'-I','-S','/usr/local/libexec/odin-runtime-identity.py'],capture_output=True);print(v.returncode)")!='0'
    subprocess.run(CREATE,check=True,timeout=10)
    second=reader.read_runtime_identity()
    assert first.incarnation!=second.incarnation and first.clock_domain==second.clock_domain
    refuse(lambda:reader.revalidate_runtime_identity(first))
    record=Path('/odin-runtime/identity.json')
    good=record.read_bytes()
    values=json.loads(good)
    invalid=[b'[]',b'not json',b'x'*513,
        json.dumps({**values,'unexpected':1}).encode(),
        json.dumps({**values,'version':True}).encode(),
        json.dumps({**values,'incarnation':'z'*32}).encode(),
        json.dumps({**values,'boot_uuid':'00000000-0000-0000-0000-000000000000'}).encode(),
        json.dumps({**values,'time_namespace_inode':values['time_namespace_inode']+1}).encode(),
        json.dumps({**values,'time_namespace_device':True}).encode(),
        good.rstrip().rstrip(b'}')+b',"version":1}',
    ]
    for content in invalid:
        record.write_bytes(content)
        refuse(reader.read_runtime_identity)
    record.write_bytes(good)
    os.chmod(record,0o644)
    refuse(reader.read_runtime_identity)
    os.chmod(record,0o444)
    os.chown(record,10001,10001)
    refuse(reader.read_runtime_identity)
    os.chown(record,0,0)
    os.chmod('/odin-runtime',0o777)
    refuse(reader.read_runtime_identity)
    os.chmod('/odin-runtime',0o755)
    os.link(record,'/odin-runtime/extra-link')
    refuse(reader.read_runtime_identity)
    os.unlink('/odin-runtime/extra-link')
    record.unlink()
    record.symlink_to('/etc/passwd')
    refuse(reader.read_runtime_identity)
    result=subprocess.run(CREATE,capture_output=True,text=True,timeout=10)
    assert result.returncode!=0 and record.is_symlink()
    record.unlink()
    os.mkfifo(record,0o444)
    refuse(reader.read_runtime_identity)
    record.unlink()
    record.write_bytes(first.content)
    os.chmod(record,0o444)
    refuse(lambda:reader.revalidate_runtime_identity(first))
    subprocess.run(CREATE,check=True,timeout=10)
    current=reader.read_runtime_identity()
    assert current.incarnation not in (first.incarnation,second.incarnation)
    creator_spec=importlib.util.spec_from_file_location('runtime_creator',CREATE[-1])
    creator=importlib.util.module_from_spec(creator_spec)
    creator_spec.loader.exec_module(creator)
    def unavailable_clock():
        raise ValueError('test clock unavailable')
    creator.clock_domain=unavailable_clock
    try:
        creator.create_identity()
    except ValueError:
        pass
    else:
        raise AssertionError('Unavailable clock accepted')
    assert not record.exists()
    refuse(reader.read_runtime_identity)
    subprocess.run(CREATE,check=True,timeout=10)
    current=reader.read_runtime_identity()
    os.rename('/odin-runtime','/odin-runtime-old')
    os.mkdir('/odin-runtime',0o755)
    record.write_bytes(current.content)
    os.chmod(record,0o444)
    refuse(lambda:reader.revalidate_runtime_identity(current))
    record.unlink()
    os.rmdir('/odin-runtime')
    os.symlink('/odin-runtime-old','/odin-runtime')
    refuse(reader.read_runtime_identity)
    print(json.dumps(dict(passed=True,scope='creator/reader only, not startup or acquisition service',
                         child_workers_share_identity=True,child_clock_namespace_inherited=True,
                         trusted_creator_invocation_changes_identity=True,
                         failed_clock_does_not_preserve_old_record=True,
                         malformed_record_cases=len(invalid))))


if __name__=='__main__':
    main()
