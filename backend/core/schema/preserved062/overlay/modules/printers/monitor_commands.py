"""Bounded local command admission to the sole authenticated monitor owner."""
import fcntl
import json
import os
import socket
import stat
import threading
import time

TIMEOUT = 4.0
MAX_FRAME = 4096


def _path():
    return os.path.join(os.environ.get('ODIN_MONITOR_COMMAND_DIR', '/data/monitor-commands'), 'mqtt.sock')


def _read(conn, deadline=None):
    deadline = deadline if deadline is not None else time.monotonic() + TIMEOUT
    data = bytearray()
    while not data.endswith(b'\n'):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        conn.settimeout(remaining)
        chunk = conn.recv(1024)
        if not chunk:
            raise ValueError('Incomplete command frame')
        data.extend(chunk)
        if len(data) > MAX_FRAME:
            raise ValueError('Oversize command frame')
    return json.loads(data)


def request_command(printer_id, action, *, on=None, print_job_id=None, object_ids=None):
    """Never retries. Once connected, failure is conservatively unknown."""
    deadline = time.monotonic() + TIMEOUT
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(TIMEOUT)
        try:
            conn.connect(_path())
        except OSError:
            return 'unavailable'
        try:
            conn.settimeout(max(0.001, deadline-time.monotonic()))
            conn.sendall(json.dumps(dict(printer_id=printer_id, action=action, on=on,
                print_job_id=print_job_id, object_ids=object_ids, deadline=deadline)).encode()+b'\n')
            result = _read(conn, deadline)
            return result if result in ('accepted', 'unavailable', 'refused', 'unknown') else 'unknown'
        except Exception:
            return 'unknown'


def valid_object_ids(value, *, empty=False):
    return (type(value) is list and (0 if empty else 1) <= len(value) <= 64
            and all(type(i) is int and 1 <= i <= 16777215 for i in value)
            and len(set(value)) == len(value))


def observe_skip_state(monitor, raw, *, retained=False):
    """Called under the status lock after job transitions. Never carry a previous print's list."""
    if getattr(monitor, '_skip_report_job_id', None) != monitor._current_job_id:
        monitor._state.pop('s_obj', None)
    monitor._skip_report_job_id = monitor._current_job_id
    if not retained and 's_obj' in raw and monitor._current_job_id is not None:
        monitor._state['s_obj'] = raw['s_obj']


def skip_state(monitor):
    state = monitor._state
    skipped = (state.get('s_obj') if monitor._current_job_id is not None
               and getattr(monitor, '_skip_report_job_id', None) == monitor._current_job_id else None)
    fun, plate, gcode = state.get('fun'), state.get('plate_idx'), state.get('gcode_state')
    return {'print_job_id': monitor._current_job_id,
            'gcode_state': gcode if type(gcode) is str and len(gcode) <= 32 else None,
            'skipped': skipped if valid_object_ids(skipped, empty=True) else None,
            'fun': fun if type(fun) is str and len(fun) <= 128 else None,
            'plate_idx': plate if type(plate) is int and 1 <= plate <= 9999 else None}


def request_skip_state(printer_id):
    """A bounded read over the existing local socket; no connection to a printer."""
    deadline = time.monotonic() + TIMEOUT
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(TIMEOUT)
            conn.connect(_path())
            conn.sendall(json.dumps(dict(printer_id=printer_id, action='skip_state', deadline=deadline)).encode()+b'\n')
            result = _read(conn, deadline)
        if type(result) is not dict or set(result) != {'print_job_id', 'gcode_state', 'skipped', 'fun', 'plate_idx'}:
            return None
        if not (result['print_job_id'] is None or type(result['print_job_id']) is int and result['print_job_id'] > 0):
            return None
        if not (result['plate_idx'] is None or type(result['plate_idx']) is int and 1 <= result['plate_idx'] <= 9999):
            return None
        for field, limit in (('gcode_state', 32), ('fun', 128)):
            if result[field] is not None and not (type(result[field]) is str and len(result[field]) <= limit):
                return None
        if result['skipped'] is not None and not valid_object_ids(result['skipped'], empty=True):
            return None
        return result
    except Exception:
        return None


def execute(monitors, message):
    if not isinstance(message, dict):
        return 'refused'
    pid, action = message.get('printer_id'), message.get('action')
    if type(pid) is not int or action not in ('light', 'resume', 'skip_objects', 'skip_state'):
        return 'refused'
    if action == 'light' and type(message.get('on')) is not bool:
        return 'refused'
    if action in ('resume', 'skip_objects') and (type(message.get('print_job_id')) is not int or message['print_job_id'] <= 0):
        return 'refused'
    if action == 'skip_objects' and not valid_object_ids(message.get('object_ids')):
        return 'refused'
    deadline = message.get('deadline')
    if type(deadline) not in (int, float) or not time.monotonic() < deadline <= time.monotonic()+TIMEOUT:
        return 'refused'
    monitor = monitors.get(pid)
    if monitor is None:
        return 'unavailable'
    if not monitor._lock.acquire(timeout=max(0, deadline-time.monotonic())):
        return 'unknown'
    try:
        transport = monitor._bambu or getattr(monitor, '_v2_adapter', None)
        if (transport is None or not transport._connected or getattr(monitor, '_transport_offline', False)
                or getattr(monitor, '_connection_retired', False)):
            return 'unavailable'
        if action == 'skip_state':
            return skip_state(monitor)
        if action == 'skip_objects' and (monitor._current_job_id != message['print_job_id'] or
                monitor._state.get('gcode_state') not in ('RUNNING', 'PAUSE', 'PAUSED')):
            return 'refused'
        if action == 'resume' and (monitor._current_job_id != message['print_job_id'] or
                monitor._state.get('gcode_state') not in ('PAUSE', 'PAUSED')):
            return 'refused'
        if time.monotonic() >= deadline:
            return 'unknown'
        payload = ({'system': {'sequence_id': '0', 'command': 'ledctrl',
            'led_node': 'chamber_light', 'led_mode': 'on' if message['on'] else 'off'}}
            if action == 'light' else {'print': {'sequence_id': '0', 'command': 'resume'}})
        if action == 'skip_objects':
            payload = {'print': {'sequence_id': '0', 'command': 'skip_objects', 'obj_list': message['object_ids']}}
        client = transport._client
        topic = 'device/'+monitor.serial+'/request'
        result = client.publish(topic, json.dumps(payload), qos=0)
        # QoS 0: local transport acceptance only, never device execution proof.
        return 'accepted' if result.rc == 0 else 'unknown'
    except Exception:
        return 'unknown'
    finally:
        monitor._lock.release()


class CommandServer:
    def __init__(self, monitors):
        path = _path()
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        directory = os.stat(os.path.dirname(path), follow_symlinks=False)
        if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid() or directory.st_mode & 0o077:
            raise PermissionError('Command directory must be private and owned')
        self.owner = open(path+'.lock', 'a')
        fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if os.path.lexists(path):
            os.unlink(path)
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.socket.bind(path)
            os.chmod(path, 0o600)
            self.socket.listen(8)
            self.socket.settimeout(0.5)
        except Exception:
            self.socket.close()
            self.owner.close()
            raise
        self.stopped = threading.Event()
        self.monitors = monitors
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stopped.is_set():
            try:
                conn, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(TIMEOUT)
                try:
                    result = execute(self.monitors, _read(conn))
                    conn.sendall(json.dumps(result).encode()+b'\n')
                except Exception:
                    pass

    def close(self):
        self.stopped.set()
        self.socket.close()
        self.thread.join(TIMEOUT + 1)
        if not self.thread.is_alive():
            self.owner.close()
