"""Internal POSIX owned-group retirement, not a dispatch/recovery authorization.

The caller must be this child's exclusive reaper: no SIGCHLD handlers,
waitpid(-1), automatic reaping or external handle access. Detached descendants
are outside this proof. No device/queue truth or durable quiescence is inferred.
"""
from dataclasses import dataclass
import math
import ctypes
import json
from pathlib import Path
import sys
import os
import signal
import select
import stat
import platform
import subprocess
import threading
import time


def _check_unreaped_child(pid):
    if hasattr(os,'waitid'):
        status=os.waitid(os.P_PID,pid,os.WEXITED|os.WNOHANG|os.WNOWAIT)
        return status is not None and status.si_pid==pid
    if sys.platform!='darwin':
        raise RuntimeError('Non-reaping child check unavailable')
    # CPython 3.11 on Darwin exposes wait constants but not os.waitid.
    # Darwin SDK sys/wait.h declares int waitid(idtype_t,id_t,siginfo_t*,int).
    # An aligned oversized buffer receives siginfo_t; only si_pid is read.
    libc=ctypes.CDLL(None,use_errno=True)
    waitid=libc.waitid
    waitid.argtypes=(ctypes.c_int,ctypes.c_uint,ctypes.c_void_p,ctypes.c_int)
    waitid.restype=ctypes.c_int
    info=(ctypes.c_longlong*64)()
    if waitid(os.P_PID,pid,ctypes.byref(info),os.WEXITED|os.WNOHANG|os.WNOWAIT)!=0:
        raise OSError(ctypes.get_errno(),'Non-reaping child ownership check failed')
    # Darwin SDK siginfo_t starts with four int fields, the fourth is si_pid.
    return ctypes.cast(ctypes.byref(info),ctypes.POINTER(ctypes.c_int))[3]==pid


@dataclass(frozen=True)
class GroupRetirement:
    group_empty: bool
    reason: str
    scope: str = 'owned process group only; no detached-child or transport proof'
    channels_closed: bool = False
    media_copies_closed: bool = True


@dataclass(frozen=True)
class GroupLaunch:
    worker_started: bool
    command_may_have_started: bool
    reason: str
    scope: str = 'worker process creation only; no command or device success proof'


@dataclass(frozen=True)
class GroupCompletion:
    worker_exited_normally: bool
    reason: str
    scope: str = 'exact owned worker exit only; no channel, group or transport proof'


MAX_WORKER_WAIT_SECONDS=300.0


class OwnedGroupStartupError(RuntimeError):
    """Retains exact ownership on failure, without exposing worker data in repr."""
    def __init__(self, group, retirement):
        super().__init__('Owned anchor startup could not be confirmed')
        self.group = group
        self.retirement = retirement


_CHANNEL_MARKERS=('{ODIN_INPUT_FD}','{ODIN_RESULT_FD}')


def _channel_identity(fd,direction):
    import fcntl
    if type(fd) is not int or fd<=2:
        raise ValueError('Invalid private worker channel')
    value=os.fstat(fd)
    if not stat.S_ISFIFO(value.st_mode) or fcntl.fcntl(fd,fcntl.F_GETFL)&os.O_ACCMODE!=direction:
        raise ValueError('Invalid private worker channel')
    if sys.platform=='darwin':
        anonymous=value.st_nlink==0
    elif sys.platform.startswith('linux'):
        anonymous=os.readlink('/proc/self/fd/'+str(fd)).startswith('pipe:[')
    else:anonymous=False
    if not anonymous:
        raise ValueError('Unsupported private worker channel')
    if sys.platform=='darwin':
        # Verified SDK pipe_fdinfo: size184, handles at160/168 (64-bit ABI).
        if ctypes.sizeof(ctypes.c_void_p)!=8 or platform.machine() not in ('arm64','x86_64'):
            raise ValueError('Unsupported private pipe identity')
        info=(ctypes.c_longlong*23)()
        call=ctypes.CDLL('/usr/lib/libproc.dylib',use_errno=True).proc_pidfdinfo
        call.argtypes=(ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_void_p,ctypes.c_int)
        call.restype=ctypes.c_int
        if call(os.getpid(),fd,6,ctypes.byref(info),184)!=184:
            raise ValueError('Private pipe identity unavailable')
        handle=ctypes.c_uint64.from_buffer(info,160).value
        peer=ctypes.c_uint64.from_buffer(info,168).value
        if not handle or not peer:raise ValueError('Private pipe peer unavailable')
        return tuple(sorted((handle,peer)))
    return value.st_dev,value.st_ino


class _PrivateChannelCleanupError(ValueError):
    def __init__(self,pending):
        super().__init__('Private channel cleanup could not be confirmed')
        self.pending=list(pending)


def _prepare_channels(argv,channels):
    copies=[]
    try:
        if channels is None:
            if any('{ODIN_' in value for value in argv):raise ValueError('Unresolved private channel')
            return list(argv),()
        if type(channels) is not dict or set(channels)!= {'input','result'}:
            raise ValueError('Invalid private worker channels')
        if any(argv.count(marker)!=1 for marker in _CHANNEL_MARKERS):
            raise ValueError('Invalid private channel arguments')
        if any('{ODIN_' in value and value not in _CHANNEL_MARKERS for value in argv):
            raise ValueError('Unresolved private channel arguments')
        identities=[]
        for name,direction in (('input',os.O_RDONLY),('result',os.O_WRONLY)):
            identity=_channel_identity(channels[name],direction)
            if identity in identities:raise ValueError('Private channels must use separate pipes')
            identities.append(identity)
            copy=os.dup(channels[name]);copies.append(copy);os.set_inheritable(copy,False)
            if _channel_identity(copy,direction)!=identity:raise ValueError('Private channel identity changed')
        replacements=dict(zip(_CHANNEL_MARKERS,map(str,copies)))
        return [replacements.get(value,value) for value in argv],tuple(copies)
    except Exception:
        pending=[]
        for fd in copies:
            try:os.close(fd)
            except OSError:pending.append(fd)
        if pending:raise _PrivateChannelCleanupError(pending) from None
        raise ValueError('Private worker channels could not be verified') from None


class OwnedSenderGroup:
    """Launch only trusted internal argv. Never pass credentials in argv.

    A strongly held private Popen keeps this direct child's PID reserved until
    retirement signals its group, then reaps it. Calls are serialized and the
    final receipt is immutable; no subsequent signal is sent after reaping.
    """
    def __init__(self, argv, *, cleanup_seconds=2.0, startup_seconds=2.0, defer_start=False, channels=None, media=None):
        required=('WNOWAIT','P_PID','WEXITED','WNOHANG','getpgid','killpg')
        if os.name!='posix' or not all(hasattr(os,name) for name in required):
            raise RuntimeError('Owned process-group supervision is unsupported')
        if signal.getsignal(signal.SIGCHLD)!=signal.SIG_DFL:
            raise RuntimeError('Exclusive child reaping is unavailable')
        if (type(cleanup_seconds) not in (int,float) or not math.isfinite(cleanup_seconds)
                or not 0<cleanup_seconds<=10):
            raise ValueError('Invalid cleanup budget')
        if (type(startup_seconds) not in (int,float) or not math.isfinite(startup_seconds)
                or not 0<startup_seconds<=10 or type(defer_start) is not bool):
            raise ValueError('Invalid startup budget or gate')
        if (type(argv) not in (list,tuple) or not argv or len(argv)>64
                or any(type(value) is not str or not value or '\x00' in value for value in argv)):
            raise ValueError('Invalid internal worker command')
        self._lock=threading.Lock()
        self._receipt=None
        self._launch=None
        self._completion=None
        self._startup_seconds=float(startup_seconds)
        self._ack_buffer=bytearray()
        self._arm_delivered=False
        self._cleanup_seconds=float(cleanup_seconds)
        self._process=None
        self._uncertain_channel_fds=[]
        self._uncertain_media_fds=[]
        self._media_capability=media
        self._uncertain_control_pipes=[]
        # A live reference is retained; the global Popen cleanup list cannot own
        # this handle. Do not poll/wait/reap it before validating/signaling.
        media_fds=();media_record=None
        try:
            if media is not None:
                from core.media_process_capability import MediaCopyCleanupError
                from core.media_process_dispatch import prepare
                if not defer_start or channels is None:raise ValueError("Media requires deferred private channels")
                try:replacements,media_fds,media_record=prepare(media,argv)
                except MediaCopyCleanupError as exc:
                    self._uncertain_media_fds=exc.pending
                    raise OwnedGroupStartupError(self,self.retire()) from None
                argv=[replacements.get(value,value) for value in argv]
            argv,private_fds=_prepare_channels(list(argv),channels)
        except _PrivateChannelCleanupError as exc:
            for fd in media_fds:
                try:os.close(fd)
                except OSError:self._uncertain_media_fds.append(fd)
            self._uncertain_channel_fds=exc.pending
            raise OwnedGroupStartupError(self,self.retire()) from None
        except Exception:
            for fd in media_fds:
                try:os.close(fd)
                except OSError:self._uncertain_media_fds.append(fd)
            if self._uncertain_media_fds:raise OwnedGroupStartupError(self,self.retire()) from None
            raise
        startup_failed=False
        try:
            envelope={'argv':argv,'fds':list(private_fds)} if private_fds else argv
            if media_record is not None:envelope['media']=media_record
            encoded=(json.dumps(envelope)+'\n').encode()
            if len(encoded)>65536:
                raise ValueError('Internal worker command is too large')
            self._process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--anchor'],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                start_new_session=True,bufsize=0,pass_fds=private_fds+media_fds)
            os.set_blocking(self._process.stdin.fileno(),False)
            os.set_blocking(self._process.stdout.fileno(),False)
            deadline=time.monotonic()+self._startup_seconds
            self._write_message(encoded,deadline)
            if self._read_ack(deadline)!=b'READY\n':
                raise RuntimeError('Invalid anchor acknowledgement')
        except Exception:
            startup_failed=True
        finally:
            for fd in private_fds:
                try:os.close(fd)
                except OSError:self._uncertain_channel_fds.append(fd)
            for fd in media_fds:
                try:os.close(fd)
                except OSError:self._uncertain_media_fds.append(fd)
        # Finalize retirement only after duplicate cleanup so its immutable
        # channel receipt includes every startup resource failure.
        if startup_failed or self._uncertain_channel_fds or self._uncertain_media_fds:
            raise OwnedGroupStartupError(self,self.retire()) from None
        if not defer_start:
            self.arm()

    def _write_message(self, message, deadline, *, arm=False):
        offset=0
        while offset<len(message):
            remaining=deadline-time.monotonic()
            if remaining<=0 or not select.select([], [self._process.stdin], [], remaining)[1]:
                raise TimeoutError('Owned IPC deadline exceeded')
            if time.monotonic()>=deadline:
                raise TimeoutError('Owned IPC deadline exceeded')
            try:
                sent=os.write(self._process.stdin.fileno(),message[offset:])
            except BlockingIOError:
                continue
            if sent<=0:
                raise RuntimeError('Owned IPC closed')
            if arm:
                self._arm_delivered=True
            offset+=sent

    def _read_ack(self, deadline):
        while True:
            if time.monotonic()>=deadline:
                raise TimeoutError('Owned IPC deadline exceeded')
            if b'\n' in self._ack_buffer:
                # No unsolicited trailing messages or partial next ACK accepted.
                if self._ack_buffer.count(b'\n')!=1 or not self._ack_buffer.endswith(b'\n'):
                    raise RuntimeError('Invalid anchor acknowledgement')
                value=bytes(self._ack_buffer)
                self._ack_buffer.clear()
                return value
            if len(self._ack_buffer)>=32:
                raise RuntimeError('Invalid anchor acknowledgement')
            remaining=deadline-time.monotonic()
            if remaining<=0 or not select.select([self._process.stdout], [], [], remaining)[0]:
                raise TimeoutError('Owned IPC deadline exceeded')
            if time.monotonic()>=deadline:
                raise TimeoutError('Owned IPC deadline exceeded')
            try:
                value=os.read(self._process.stdout.fileno(),33-len(self._ack_buffer))
            except BlockingIOError:
                continue
            if not value:
                raise RuntimeError('Owned IPC closed')
            self._ack_buffer.extend(value)

    def arm(self):
        """Allow one worker launch. Register the exact attempt BEFORE this call."""
        with self._lock:
            if self._launch is not None:
                return self._launch
            if self._receipt is not None or self._completion is not None:
                self._launch=GroupLaunch(False,False,'Owned group already retired or unavailable')
                return self._launch
            try:
                if (signal.getsignal(signal.SIGCHLD)!=signal.SIG_DFL
                        or self._process.returncode is not None
                        or _check_unreaped_child(self._process.pid)
                        or os.getpgid(self._process.pid)!=self._process.pid):
                    raise RuntimeError('Owned anchor unavailable')
                if self._media_capability is not None:self._media_capability.guard.assert_owned()
                deadline=time.monotonic()+self._startup_seconds
                self._write_message(b'ARM\n',deadline,arm=True)
                ack=self._read_ack(deadline)
                if time.monotonic()>=deadline:
                    raise TimeoutError('Owned IPC deadline exceeded')
                if ack==b'STARTED\n':
                    self._launch=GroupLaunch(True,True,'Owned worker process created')
                elif ack==b'START_FAILED\n':
                    self._launch=GroupLaunch(False,False,'Owned worker process creation failed')
                else:
                    raise RuntimeError('Invalid anchor acknowledgement')
            except Exception:
                self._launch=GroupLaunch(False,self._arm_delivered,'Owned worker startup could not be confirmed')
            return self._launch

    def wait_worker(self, deadline):
        """Confirm the exact worker exited zero, not full sender cleanup."""
        with self._lock:
            if self._completion is not None:
                return self._completion
            self._completion=GroupCompletion(False,'Owned worker completion could not be confirmed')
            try:
                if (type(deadline) not in (int,float) or not math.isfinite(deadline)
                        or not 0<deadline-time.monotonic()<=MAX_WORKER_WAIT_SECONDS
                        or self._receipt is not None or self._uncertain_channel_fds or self._uncertain_media_fds
                        or self._launch is None or not self._launch.worker_started):
                    return self._completion
                if (signal.getsignal(signal.SIGCHLD)!=signal.SIG_DFL
                        or self._process is None or self._process.returncode is not None
                        or _check_unreaped_child(self._process.pid)
                        or os.getpgid(self._process.pid)!=self._process.pid):
                    return self._completion
                self._write_message(b'WAIT\n',deadline)
                ack=self._read_ack(deadline)
                if time.monotonic()>=deadline:
                    return self._completion
                if ack==b'EXIT_OK\n':
                    self._completion=GroupCompletion(True,'Exact owned worker reaped with zero exit status')
            except Exception:
                pass
            return self._completion

    def retire(self):
        with self._lock:
            if self._receipt is not None:
                return self._receipt
            process=self._process
            if process is None:
                self._receipt=GroupRetirement(False,'Owned anchor creation could not be confirmed',media_copies_closed=not self._uncertain_media_fds)
                return self._receipt
            deadline=time.monotonic()+self._cleanup_seconds
            group_empty=False
            reason='Owned process cleanup could not be confirmed'
            try:
                if signal.getsignal(signal.SIGCHLD)!=signal.SIG_DFL or process.returncode is not None:
                    raise RuntimeError('Child ownership unavailable')
                # WNOWAIT checks the kernel parent-child relationship without
                # reaping an exited leader or freeing its PID for reuse.
                if _check_unreaped_child(process.pid):
                    # Unexpected anchor exit is not controlled retirement. Keep
                    # uncertainty even when its old group happens to be empty.
                    raise RuntimeError('Owned anchor exited unexpectedly')
                group=os.getpgid(process.pid)
                if group!=process.pid:
                    raise RuntimeError('Owned session differs')
                os.killpg(group,signal.SIGKILL)
                process.wait(timeout=max(0.001,deadline-time.monotonic()))
                # Never signal this group again after wait/reap. If a reused or
                # zombie group exists, refuse instead of interpreting its members.
                while True:
                    try:
                        os.killpg(group,0)
                    except ProcessLookupError:
                        group_empty=True
                        reason='Owned leader reaped and group absent'
                        break
                    if time.monotonic()>=deadline:
                        raise RuntimeError('Group absence unconfirmed')
                    time.sleep(min(0.01,max(0,deadline-time.monotonic())))
            except Exception:
                # No exception text, argv, credentials or arbitrary worker output.
                pass
            # Private IPC is closed even on uncertainty; the anchor stays live
            # on EOF until explicit owned retirement. No EOF means permission.
            channels_closed=not self._uncertain_channel_fds
            for pipe in (process.stdin,process.stdout):
                try:
                    pipe.close()
                    if pipe.closed is not True:
                        raise RuntimeError('Control channel close unconfirmed')
                except Exception:
                    channels_closed=False
                    self._uncertain_control_pipes.append(pipe)
            self._receipt=GroupRetirement(group_empty,reason,channels_closed=channels_closed,
                media_copies_closed=not self._uncertain_media_fds)
            return self._receipt

    def __enter__(self):
        return self

    def __exit__(self,*args):
        self.retire()


def _anchor():
    # Fixed private protocol only. Worker output never enters this pipe.
    worker=None
    private_fds=();media_fds=()
    try:
        line=sys.stdin.buffer.readline(65537)
        argv=json.loads(line)
        if type(argv) is dict:
            if set(argv) not in ({'argv','fds'},{'argv','fds','media'}) or type(argv['fds']) is not list or len(argv['fds'])!=2:
                raise RuntimeError('Invalid private channels')
            if 'media' in argv:
                from media_process_dispatch import envelope_command
                media_fds,fixed,markers=envelope_command(argv['media'])
                replacements=dict(zip(('{ODIN_INPUT_FD}','{ODIN_RESULT_FD}',*markers),map(str,tuple(argv['fds'])+media_fds)))
                if argv['argv']!=[replacements.get(value,value) for value in fixed]:raise RuntimeError('Invalid media worker')
            private_fds=tuple(argv['fds']);argv=argv['argv']
            identities=(_channel_identity(private_fds[0],os.O_RDONLY),_channel_identity(private_fds[1],os.O_WRONLY))
            if identities[0]==identities[1]:raise RuntimeError('Private channel identities overlap')
        if (len(line)>65536 or not line.endswith(b'\n') or type(argv) is not list
                or not argv or len(argv)>64
                or any(type(value) is not str or not value or '\x00' in value for value in argv)):
            raise RuntimeError('Invalid configuration')
        sys.stdout.buffer.write(b'READY\n');sys.stdout.buffer.flush()
        if sys.stdin.buffer.readline(5)!=b'ARM\n':
            raise RuntimeError('Permission absent')
        try:
            worker=subprocess.Popen(argv,stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,pass_fds=private_fds+media_fds)
        except Exception:
            sys.stdout.buffer.write(b'START_FAILED\n');sys.stdout.buffer.flush()
        else:
            sys.stdout.buffer.write(b'STARTED\n');sys.stdout.buffer.flush()
    except Exception:
        pass
    finally:
        for fd in private_fds:
            try:os.close(fd)
            except OSError:pass
    # Stable anchor remains under exclusive parent ownership after errors/EOF.
    # EXIT is solicited only after parent consumes STARTED, never coalesced
    # unsolicited with the strict launch acknowledgement.
    wait_buffer=bytearray();wait_requested=False;completion_sent=False;control_valid=True
    try:os.set_blocking(sys.stdin.fileno(),False)
    except Exception:control_valid=False
    while True:
        try:
            status=worker.poll() if worker is not None else None
        except Exception:
            status=None;control_valid=False
        if control_valid:
            try:
                if select.select([sys.stdin],[],[],.05)[0]:
                    chunk=os.read(sys.stdin.fileno(),6-len(wait_buffer))
                    if not chunk:
                        control_valid=False
                    elif wait_requested or completion_sent:
                        control_valid=False
                    else:
                        wait_buffer.extend(chunk)
                        if len(wait_buffer)>5 or not b'WAIT\n'.startswith(wait_buffer):
                            control_valid=False
                        elif wait_buffer==b'WAIT\n':
                            wait_requested=True
                if control_valid and wait_requested and not completion_sent and status is not None:
                    acknowledgement=b'EXIT_OK\n' if type(status) is int and status==0 else b'EXIT_FAILED\n'
                    sys.stdout.buffer.write(acknowledgement);sys.stdout.buffer.flush()
                    completion_sent=True
            except Exception:
                control_valid=False
        else:
            time.sleep(.05)


if __name__=='__main__' and sys.argv[1:]==['--anchor']:
    _anchor()
