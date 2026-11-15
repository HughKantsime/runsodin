"""Owned server/simulator lifecycle for run_live.sh. No product response stubs."""

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time

from runtime import ROOT, refuse_database_environment, create_test_app, allow_simulator
from seed_fixture import seed


def run(package, out):
    refuse_database_environment(out)
    if not (package / "test/live/contracts.test.ts").is_file() or not (package / "node_modules").is_dir():
        raise ValueError("The MCP package needs its live suite and existing node_modules; nothing is installed automatically.")
    sys.path.insert(0, str(ROOT))
    from ops.hardware_certification.simulators import StatefulHardwareHttpPeer

    def stop(signum, _frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    peer = StatefulHardwareHttpPeer("moonraker", "contract.gcode", b"")
    peer.state = "printing"
    # The peer owns a port allocated by the OS; it never writes ODIN's DB.
    try:
        peer.peer.start()
    except PermissionError:
        raise RuntimeError("Cannot bind the simulator on 127.0.0.1; run this harness outside the localhost-restricted sandbox.") from None
    try:
        return run_with_peer(package, out, peer)
    finally:
        peer.peer.stop()


def run_with_peer(package, out, peer):
    import uvicorn

    child = None
    thread = None
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        printer_port = int(peer.base_url.rsplit(":", 1)[1])
        fixtures = seed(out, printer_port)
        allow_simulator(printer_port)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        app = create_test_app()
        config = uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False, log_level="warning")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        try:
            thread.start()
            deadline = time.monotonic() + 30
            while not server.started:
                if not thread.is_alive() or time.monotonic() > deadline:
                    raise RuntimeError("The disposable backend did not start.")
                time.sleep(0.05)
            # Token exists only in this child's environment, never in argv or the
            # parent environment. Discard inherited package credentials/proxies.
            env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR")}
            env.update({
                "ODIN_BASE_URL": f"http://127.0.0.1:{port}",
                "ODIN_API_KEY": (out / "token").read_text(),
                "ODIN_LIVE_FIXTURES": json.dumps(fixtures),
                "ODIN_LIVE_DISPOSABLE": "1", "ODIN_LIVE_SIMULATED_PRINTERS": "1",
                "npm_config_offline": "true", "npm_config_update_notifier": "false",
                "npm_config_ignore_scripts": "true", "npm_config_cache": str(out / "npm-cache"),
                "npm_config_userconfig": str(out / "npmrc-user"), "npm_config_globalconfig": str(out / "npmrc-global"),
            })
            child = subprocess.Popen(["npm", "run", "test:live"], cwd=package, env=env, start_new_session=True)
            code = child.wait()
            return code if code >= 0 else 128 - code
        finally:
            if child is not None:
                # npm can leave its node child alive after npm itself exits.
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
            server.should_exit = True
            if thread is not None:
                thread.join(timeout=10)
            from core.db import engine
            engine.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(run(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()))
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
    except Exception as error:
        # Never print exception parameters that might contain generated secrets.
        print(f"Live harness setup failed ({type(error).__name__}).", file=sys.stderr)
        raise SystemExit(2)
