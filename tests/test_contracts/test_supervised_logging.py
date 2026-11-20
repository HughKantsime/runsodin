"""Every configured daemon sends both output streams through the same local-time formatter."""
import configparser
import importlib
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PREFIX = "python3 -m core.supervised_log "


def programs():
    for path in sorted((ROOT / "docker").glob("supervisord*.conf")):
        conf = configparser.RawConfigParser()
        conf.read(path)
        for section in conf.sections():
            if section.startswith("program:"):
                yield path.name, section.split(":")[1], conf[section]["command"]


@pytest.mark.parametrize("conf,name,command", programs())
def test_program_setup_dates_prints_tracebacks_and_access_lines(conf, name, command):
    from io import BytesIO

    assert command.startswith(PREFIX), (conf, name)
    setup = importlib.import_module(shlex.split(command)[2])
    original = b"2026-01-02 03:04:05,006 [monitor] existing line\n"
    output = BytesIO()
    setup.timestamp_lines(BytesIO(original + b"INFO: access\n  trace\n"), output, name)
    first, access, trace = output.getvalue().splitlines()
    assert first == original.rstrip()
    prefix = rb"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \[" + name.encode() + rb"\] "
    assert re.fullmatch(prefix + b"INFO: access", access)
    assert re.fullmatch(prefix + b"  trace", trace)


def test_supervised_exception_exit_code_and_both_streams():
    result = subprocess.run([sys.executable, "-m", "core.supervised_log", sys.executable, "-u", "-c",
                             "print('starting'); raise RuntimeError('stopped')"],
                            cwd=ROOT / "backend", env={**os.environ, "SUPERVISOR_PROCESS_NAME": "test"},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    messages = [line.split(" [test] ")[1] for line in result.stdout.splitlines()]
    assert messages[0] == "starting" and messages[-1] == "RuntimeError: stopped"
    assert "Traceback (most recent call last):" in messages


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


def test_supervised_listeners_use_the_container_port_and_warn_once(monkeypatch, caplog):
    import uvicorn
    from core import api_server

    monkeypatch.setenv("PORT", "18080")
    monkeypatch.setenv("SUPERVISOR_PROCESS_NAME", "backend")
    launches = []
    monkeypatch.setattr(uvicorn, "main", lambda args: launches.append(args))
    for conf, name, command in programs():
        if name == "backend":
            args = shlex.split(command.removeprefix(PREFIX))
            assert args[:3] == ["python3", "-m", "core.api_server"], conf
            monkeypatch.setattr(sys, "argv", args[2:])
            caplog.clear()
            api_server.main()
            assert launches[-1] == ["main:app", "--port", str(api_server.CONTAINER_API_PORT), *args[3:]]
            assert len([msg for msg in caplog.messages if msg.startswith("PORT ")]) == 1
