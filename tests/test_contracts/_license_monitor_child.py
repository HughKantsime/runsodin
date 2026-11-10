"""Child process for test_license_monitor_b2c: a real monitor daemon loop whose
printer sessions are fakes that block forever holding their lock and an open socket.

Environment: B2C_MODE (a monitor module name, or 'bambu' for MQTTMonitorDaemon),
B2C_LOG (event log), B2C_FLAG (while it exists, policy reads hang: unreadable),
B2C_PEER_PORT (the parent's TCP listener each session connects to),
B2C_PUBLIC_KEY (the test license issuer), B2C_DIE (optional printer id whose session
thread ends right after it starts: an unexpected thread death), plus DATABASE_URL and LICENSE_DIR.
"""
import importlib
import os
import socket
import threading
import time

LOG, FLAG, MODE = os.environ['B2C_LOG'], os.environ['B2C_FLAG'], os.environ['B2C_MODE']
PEER = int(os.environ['B2C_PEER_PORT'])
DIE = int(os.environ.get('B2C_DIE') or 0)


def note(line):
    with open(LOG, 'a') as f:
        f.write(f'{time.time():.3f} {line}\n')


import license_manager  # noqa: E402

license_manager.ODIN_PUBLIC_KEY = os.environ['B2C_PUBLIC_KEY']

from core import license_lock, license_monitor  # noqa: E402


def _queried(real):
    def read(*args, **kwargs):
        note('QUERY')
        while os.path.exists(FLAG):  # unreadable policy: the read hangs until the flag goes
            time.sleep(0.05)
        return real(*args, **kwargs)
    return read


license_lock.snapshot = _queried(license_lock.snapshot)
license_lock.stored_snapshot = _queried(license_lock.stored_snapshot)


class Session:
    """Blocked forever in its own thread while holding its lock and a TCP connection."""

    def __init__(self, pid):
        self.pid = pid
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        with self.lock:
            self.sock = socket.create_connection(('127.0.0.1', PEER))
            self.sock.sendall(f'{self.pid}\n'.encode())
            note(f'start {self.pid}')
            if self.pid == DIE:
                return  # the thread ends; its socket stays open (only the process exit closes it)
            threading.Event().wait()

    def start(self):
        self.thread.start()

    def is_alive(self):
        return self.thread.is_alive()

    def stop(self):
        with self.lock:  # never returns while the session is blocked
            pass


class FakeBambu(Session):
    def __init__(self, printer_id, name, ip, serial, access_code):
        super().__init__(printer_id)
        self.name, self.ip = name, ip
        self._transport_offline, self._bambu, self._last_heartbeat = False, None, 0

    def connect(self):
        note(f'connect {self.pid}')
        if self.ip == 'hang':
            threading.Event().wait()  # a synchronous connect that never returns
        self.start()
        return True

    def disconnect(self):
        self.stop()


if MODE == 'bambu':
    import core.monitor_bootstrap as bootstrap
    import modules.printers.monitor_commands as commands
    from modules.printers.monitors import mqtt_monitor

    class NoCommandChannel:
        def __init__(self, monitors):
            raise OSError('no command channel in this test')

    bootstrap.bootstrap_monitor_authority = lambda: None
    commands.CommandServer = NoCommandChannel
    mqtt_monitor.PrinterMonitor = FakeBambu
    daemon = mqtt_monitor.MQTTMonitorDaemon()
    daemon._last_camera_resolution = time.time()  # no go2rtc work in this test
    daemon.start()
else:
    module = importlib.import_module(f'modules.printers.monitors.{MODE}')
    stop = threading.Event()

    def factory(pid, *args):
        return Session(pid)

    if license_lock.enabled():
        license_monitor.monitor_loop(module.engine, module._inventory, factory, MODE, stop)
    else:
        def start_all():
            sessions = [Session(row['id']) for row in module._rows()]
            for session in sessions:
                session.start()
            return sessions
        license_monitor.run_monitor_process(module.engine, module._inventory, factory, start_all, MODE, stop)
