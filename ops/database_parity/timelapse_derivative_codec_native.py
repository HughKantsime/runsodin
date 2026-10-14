"""Owned Linux codec fixture; no database, production media or hardware."""
import json,os,subprocess,tempfile,time
from fractions import Fraction
from pathlib import Path
from core.timelapse_derivative_worker import probe,transform,collect,command
from core.timelapse_derivative_timing import frame_plan
from core.timelapse_recipes import make_recipe

with tempfile.TemporaryDirectory(prefix='odin-derivative-codec-') as directory:
    source=Path(directory)/'source.mp4'
    colors=[20+i*25 for i in range(7)]
    raw=b''.join(bytes([v])*(64*64)+bytes([128])*(64*64//2) for v in colors)
    subprocess.run(['/usr/bin/ffmpeg','-nostdin','-v','error','-threads','2','-f','rawvideo','-pixel_format','yuv420p','-video_size','64x64',
        '-framerate','30','-i','pipe:0','-c:v','libx264','-crf','0','-pix_fmt','yuv420p',str(source)],input=raw,check=True)
    os.chmod(source,0o600)
    guard=os.open(Path(directory)/'guard',os.O_RDWR|os.O_CREAT|os.O_EXCL,0o600)
    fd=os.open(source,os.O_RDONLY)
    try:
        deadline=time.monotonic()+120
        metadata=probe(fd,guard,deadline)
        assert metadata=={'count':7,'width':64,'height':64}
        before=source.read_bytes()
        recipes=[make_recipe('speed',multiplier=v) for v in (.5,1,1.5,2,4,8)]+[make_recipe('trim',start_seconds=.04,end_seconds=.2)]
        for index,recipe in enumerate(recipes):
            output=os.open(Path(directory)/f'out{index}.mp4',os.O_RDWR|os.O_CREAT|os.O_EXCL,0o600)
            try:
                indices=frame_plan(recipe,7)
                transform(fd,output,guard,metadata,indices,deadline)
                found=probe(output,guard,deadline)
                assert found==dict(metadata,count=len(indices))
                decoded=collect(command('ffmpeg',['-v','error','-xerror','-protocol_whitelist','file','-i','/proc/self/fd/'+str(output),
                    '-pix_fmt','yuv420p','-f','rawvideo','-']),fds=(guard,output),deadline=deadline,maximum=32768*6144)
                assert len(decoded)==len(indices)*6144
                for j,i in enumerate(indices):
                    y=decoded[j*6144:j*6144+4096]
                    assert abs(sum(y)/len(y)-colors[i])<5
                assert source.read_bytes()==before
                print(json.dumps({'marker':'codec_recipe','kind':recipe.kind,'recipe':recipe.columns(),'source_count':7,
                    'output_count':len(indices),'duration':str(Fraction(len(indices),30)),'sampled_indices':list(indices),'source_unchanged':True}),flush=True)
            finally:os.close(output)
        from core.timelapse_media_lock import acquire_media_guard
        from core.media_derivative_capability import MediaDerivativeCapability,worker_command,complete_derivative_transport
        from core.timelapse_media_worker import file_facts
        from core.sender_process_group import OwnedSenderGroup
        output=os.open(Path(directory)/'transport.mp4',os.O_RDWR|os.O_CREAT|os.O_EXCL,0o600)
        channels=[];group=None
        try:
            with acquire_media_guard(directory,1) as live_guard:
                from core.media_probe_capability import probe_owned_media
                preflight=probe_owned_media(live_guard,fd,file_facts(fd),deadline,'b'*32)
                assert preflight==metadata
                print(json.dumps({'marker':'owned_readonly_preflight','frames':7,'no_output_descriptor':True}),flush=True)
                cap=MediaDerivativeCapability.from_receipts(live_guard,fd,output,file_facts(fd),file_facts(output))
                a,b=os.pipe();c,d=os.pipe();channels=[a,b,c,d]
                group=OwnedSenderGroup(worker_command(),defer_start=True,channels={'input':a,'result':d},media=cap)
                for channel in (a,d):channels.remove(channel);os.close(channel)
                recipe=make_recipe('speed',multiplier=8)
                manifest={'operation_id':'a'*32,'kind':'speed','recipe':recipe.columns(),'source':file_facts(fd),'deadline':deadline}
                result=complete_derivative_transport(group,manifest,input_writer=b,result_reader=c,source_fd=fd,output_fd=output,deadline=deadline)
                assert result['frame_count']==1 and result['source_frame_count']==7
                assert source.read_bytes()==before
                receipt=group.retire()
                assert receipt.group_empty and receipt.channels_closed and receipt.media_copies_closed
                print(json.dumps({'marker':'owned_typed_transport','output_frames':1,'group_empty':True,'channels_closed':True,'copies_closed':True}),flush=True)
                readonly=os.open(Path(directory)/'transport.mp4',os.O_RDONLY)
                try:
                    actual=probe_owned_media(live_guard,readonly,file_facts(readonly),deadline,'c'*32)
                    assert actual==dict(metadata,count=1)
                    for multiplier in (.5,8):
                        plan=frame_plan(make_recipe('speed',multiplier=multiplier),actual['count'])
                        assert len(plan)==(2 if multiplier==.5 else 1)
                    assert len(frame_plan(make_recipe('trim',start_seconds=0,end_seconds=.03),actual['count']))==1
                    print(json.dumps({'marker':'one_frame_output_reusable_probe','count':1,'readonly':True}),flush=True)
                finally:os.close(readonly)
        finally:
            if group is not None:group.retire()
            for channel in channels:os.close(channel)
            os.close(output)
        import core.timelapse_derivative_worker as worker
        import resource,sys
        # An owned smaller cap exercises the exact production preexec hard-limit
        # function without allocating32GiB. Production MAX_BYTES remains pinned.
        actual_max=worker.MAX_BYTES;worker.MAX_BYTES=4096
        resource_fd=os.open(Path(directory)/'limit.bin',os.O_RDWR|os.O_CREAT|os.O_EXCL,0o600)
        try:
            process=worker.launch([sys.executable,'-c',"import os,resource,sys; f=int(sys.argv[1]); assert resource.getrlimit(resource.RLIMIT_FSIZE)==(4096,4096); os.write(f,b'x'*8192); os.write(f,b'x')",str(resource_fd)],fds=(resource_fd,),stdout=subprocess.DEVNULL)
            process.wait(timeout=5)
            assert process.returncode!=0 and os.fstat(resource_fd).st_size==4096
            worker.stop(process)
            print(json.dumps({'marker':'actual_during_write_hard_fsize','test_cap':4096,'size_at_refusal':4096}),flush=True)
        finally:worker.MAX_BYTES=actual_max;os.close(resource_fd)
        failed=False
        try:worker.collect([sys.executable,'-c','import time; time.sleep(5)'],fds=(),deadline=time.monotonic()+.05,maximum=0)
        except ValueError:failed=True
        assert failed
        print(json.dumps({'marker':'actual_shared_deadline_child_cleanup','expired_probe_refused':True}),flush=True)
        print(json.dumps({'marker':'codec_complete','recipes':7,'owned_fixture_cleanup':'context-finally'}),flush=True)
    finally:os.close(fd);os.close(guard)
