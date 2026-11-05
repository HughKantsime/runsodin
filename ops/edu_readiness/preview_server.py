"""Serve the built frontend for one browser check and leave nothing behind.

A browser check must only ever reach a server this run started. A leftover `vite preview`
from an earlier run answers on the same port with the earlier build, so a broken preview
could pass. npm doesn't stop its script's children when it is signalled, so the server gets
its own process group and the whole group is stopped.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
READY_SECONDS = 30.0
STOP_SECONDS = 5.0
ANSI = re.compile(r"\x1b\[[0-9;]*m")


class PreviewError(RuntimeError):
    """The check can't be trusted to reach a server this run owns."""


def local_address(base_url: str) -> tuple[str, int]:
    parsed = urlsplit(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.port is None:
        raise ValueError("EDU_FRONTEND_URL must be a local HTTP preview URL with a port")
    return parsed.hostname, parsed.port


def vite_command(port: int) -> list[str]:
    return [
        "npm", "--prefix", str(ROOT / "frontend"), "run", "preview", "--",
        "--host", "127.0.0.1", "--port", str(port), "--strictPort",
    ]


def _listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def _serves(host: str, port: int) -> bool:
    connection = http.client.HTTPConnection(host, port, timeout=2)
    try:
        connection.request("GET", "/")
        return connection.getresponse().status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def _read_log(log) -> str:
    return ANSI.sub("", os.pread(log.fileno(), 1024 * 1024, 0).decode("utf-8", errors="replace"))


def _wait_until_ready(process: subprocess.Popen, log, host: str, port: int, deadline_seconds: float) -> None:
    # vite announces its URL only after binding, and --strictPort makes a taken port fatal,
    # so the announcement is what shows that the answering server is this one.
    announced = re.compile(rf"http://(?:127\.0\.0\.1|localhost):{port}/")
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise PreviewError(f"preview exited with {process.returncode}: {_read_log(log)[-2000:]}")
        if announced.search(_read_log(log)) and _serves(host, port):
            return
        time.sleep(0.1)
    raise PreviewError(f"preview was not ready within {deadline_seconds:.0f}s: {_read_log(log)[-2000:]}")


def _group_alive(process: subprocess.Popen) -> bool:
    process.poll()  # reap the leader so a zombie doesn't count as the group being alive
    try:
        os.killpg(process.pid, 0)
    except (ProcessLookupError, PermissionError):  # macOS answers EPERM for a group of zombies
        return False
    return True


def _group_gone_within(process: subprocess.Popen, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _group_alive(process):
            return True
        time.sleep(0.05)
    return False


def _stop_group(process: subprocess.Popen, stop_seconds: float) -> None:
    for chosen in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, chosen)
        if _group_gone_within(process, stop_seconds):
            break
    try:
        process.wait(timeout=stop_seconds)
    except subprocess.TimeoutExpired as exc:
        raise PreviewError(f"preview {process.pid} did not exit after SIGKILL") from exc


@contextlib.contextmanager
def _stop_on_termination() -> Iterator[None]:
    """A terminated gate still runs its cleanup; Python's default would skip `finally`."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def terminate(number, _frame):
        raise SystemExit(128 + number)

    previous = {number: signal.signal(number, terminate) for number in (signal.SIGTERM, signal.SIGHUP)}
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


@contextlib.contextmanager
def owned_preview(
    base_url: str, *, command: list[str] | None = None,
    ready_seconds: float = READY_SECONDS, stop_seconds: float = STOP_SECONDS,
) -> Iterator[None]:
    """Start a preview for the duration of the block, or refuse if the port is already taken."""
    host, port = local_address(base_url)
    if _listening(host, port):
        raise PreviewError(f"{host}:{port} already has a listener; refusing to test a server this run did not start")
    with tempfile.TemporaryFile() as log, _stop_on_termination():
        process = subprocess.Popen(
            command or vite_command(port),
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            env=dict(os.environ, NO_COLOR="1"), start_new_session=True,
        )
        try:
            _wait_until_ready(process, log, host, port, ready_seconds)
            yield
        finally:
            _stop_group(process, stop_seconds)
        if _listening(host, port):
            raise PreviewError(f"{host}:{port} still has a listener after the preview stopped")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required after --")
    try:
        with owned_preview(args.url):
            return subprocess.run(command, check=False).returncode
    except PreviewError as exc:
        print(f"preview: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
