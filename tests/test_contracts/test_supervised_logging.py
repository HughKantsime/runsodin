"""Exercise the logging startup of every program declared in any supervisor configuration."""
import ast
import configparser
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
DATE = r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} "


def programs():
    for path in sorted((ROOT / "docker").glob("supervisord*.conf")):
        conf = configparser.RawConfigParser()
        conf.read(path)
        for section in conf.sections():
            if section.startswith("program:"):
                yield path.name, section.split(":")[1], conf[section]["command"]


def run_python(code, *args, **env):
    return subprocess.run([sys.executable, "-c", code, *args], cwd=BACKEND,
                          env={**os.environ, **env}, capture_output=True, text=True, timeout=15)


# Exit at the actual setup call, before any service opens a database or starts its loop.
DAEMON_PROBE = """
import logging, runpy, sys, threading
from core import supervised_log
setup = supervised_log.setup_logging
stdout = sys.stdout
def probe():
    setup()
    assert sys.stdout is stdout
    logging.getLogger("probe").info("starting")
    def crash():
        raise RuntimeError("thread stopped")
    thread = threading.Thread(target=crash, name="monitor-worker")
    thread.start()
    thread.join()
    raise RuntimeError("daemon stopped")
supervised_log.setup_logging = probe
runpy.run_module(sys.argv[1], run_name="__main__")
"""


@pytest.mark.parametrize("conf,name,command", programs())
def test_program_setup_dates_prints_tracebacks_and_access_lines(conf, name, command):
    args = shlex.split(command)
    if name == "go2rtc":
        assert args == ["/usr/local/bin/go2rtc", "-config", "/app/go2rtc/go2rtc.yaml"]
        return
    assert args[:2] == ["python3", "-m"] and "core.supervised_log" not in args, conf
    if name == "backend":
        assert args[2:4] == ["uvicorn", "main:app"]
        result = run_python("""
import logging, sys
from uvicorn import Config
Config("main:app", log_config=sys.argv[1])
logging.getLogger("odin.api").info("starting")
logging.getLogger("uvicorn.error").info("ready")
logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "client", "GET", "/", "1.1", 200)
""", args[args.index("--log-config") + 1])
        assert result.returncode == 0, result.stderr
        service, error = result.stderr.splitlines()
        assert re.fullmatch(DATE + r"\[odin.api\] starting", service)
        assert re.fullmatch(DATE + r"INFO: +ready", error)
        assert re.fullmatch(DATE + r'INFO: +client - "GET / HTTP/1.1" 200 OK', result.stdout.strip())
        return
    assert len(args) == 3
    module = args[2]
    source = (BACKEND / (module.replace(".", "/") + ".py")).read_text()
    calls = {node.func.id for node in ast.walk(ast.parse(source))
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    # Fail before launching an unfamiliar service without the probe's stop point.
    assert "setup_logging" in calls and "print" not in calls, (conf, name)
    result = run_python(DAEMON_PROBE, module, SUPERVISOR_PROCESS_NAME=name)
    assert result.returncode == 1 and result.stdout == "", result.stderr
    lines = result.stderr.splitlines()
    assert any(re.fullmatch(DATE + r"\[probe\] starting", line) for line in lines), result.stderr
    assert all(re.match(DATE + r"\[[^]]+\] ", line) for line in lines), result.stderr
    for message in ("Traceback (most recent call last):", "RuntimeError: thread stopped", "RuntimeError: daemon stopped"):
        assert any(line.endswith(message) for line in lines), result.stderr


def test_supervised_exception_exit_code_and_both_streams():
    result = run_python("""
from core.supervised_log import setup_logging
setup_logging()
print("ordinary stdout")
raise RuntimeError("daemon stopped")
""")
    assert result.returncode == 1 and result.stdout == "ordinary stdout\n"
    assert re.search(DATE + r"\[[^]]+\] RuntimeError: daemon stopped$", result.stderr, re.M)


@pytest.mark.parametrize("present", [True, False])
def test_settings_load_and_warn_only_for_present_unused_names(monkeypatch, caplog, present):
    import logging
    from core.config import Settings, warn_unused_settings

    names = ("HOST", "MCP_ENABLED", "SCHEDULER_HORIZON_DAYS")
    for name in (*names, "PORT"):
        if present:
            monkeypatch.setenv(name, "invalid")
        else:
            monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)
    assert not any(hasattr(settings, name.lower()) for name in (*names, "PORT"))
    warn_unused_settings(logging.getLogger("odin.api"))
    assert [msg for msg in caplog.messages if not msg.startswith("PORT ")] == (
        [f"{name} is set but has no effect in ODIN; remove it." for name in names] if present else [])
    assert sum(msg.startswith("PORT ") for msg in caplog.messages) == int(present)


def test_supervised_listeners_use_the_container_port_and_warn_once():
    from core.ports import CONTAINER_API_PORT

    dockerfile = (ROOT / "Dockerfile").read_text()
    assert str(CONTAINER_API_PORT) in re.search(r"^EXPOSE (.+)$", dockerfile, re.M)[1].split()
    assert int(re.search(r"http://localhost:(\d+)/health/ready", dockerfile)[1]) == CONTAINER_API_PORT

    # Uvicorn configures logging once in the listener process and again in each spawned worker.
    for conf, name, command in programs():
        if name != "backend":
            continue
        args = shlex.split(command)
        assert args[:4] == ["python3", "-m", "uvicorn", "main:app"], conf
        assert int(args[args.index("--port") + 1]) == CONTAINER_API_PORT
        result = run_python("""
import multiprocessing, sys
from uvicorn import Config
config = Config("main:app", log_config=sys.argv[1])
if int(sys.argv[2]) > 1:
    worker = multiprocessing.get_context("spawn").Process(target=config.configure_logging)
    worker.start()
    worker.join(5)
    assert worker.exitcode == 0
""", args[args.index("--log-config") + 1], args[args.index("--workers") + 1],
                            SUPERVISOR_PROCESS_NAME="backend", PORT="18080", HOST="",
                            MCP_ENABLED="", SCHEDULER_HORIZON_DAYS="")
        assert result.returncode == 0, result.stderr
        for variable in ("HOST", "PORT", "MCP_ENABLED", "SCHEDULER_HORIZON_DAYS"):
            assert result.stderr.count(f"[odin.api] {variable} ") == 1, conf
