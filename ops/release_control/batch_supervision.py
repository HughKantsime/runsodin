"""Process-tree supervision and log/JUnit inspection helpers for contract_batches."""

from __future__ import annotations

import os
import signal
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path


def _send_signal(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _descendants_via_ps(root_pid: int) -> list[int] | None:
    try:
        completed = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid="],
            capture_output=True, text=True, check=False, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    children: dict[int, list[int]] = {}
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)
    order: list[int] = []
    seen = {root_pid}
    queue = [root_pid]
    while queue:
        current = queue.pop(0)
        for child in children.get(current, ()):
            if child not in seen:
                seen.add(child)
                order.append(child)
                queue.append(child)
    return order


def _descendants_via_pgrep(root_pid: int) -> list[int]:
    order: list[int] = []
    seen = {root_pid}
    queue = [root_pid]
    while queue:
        current = queue.pop(0)
        try:
            completed = subprocess.run(
                ["pgrep", "-P", str(current)],
                capture_output=True, text=True, check=False, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        for token in completed.stdout.split():
            try:
                child = int(token)
            except ValueError:
                continue
            if child not in seen:
                seen.add(child)
                order.append(child)
                queue.append(child)
    return order


def _descendants(root_pid: int) -> list[int]:
    result = _descendants_via_ps(root_pid)
    return _descendants_via_pgrep(root_pid) if result is None else result


def kill_process_tree(process: subprocess.Popen[object], *, max_rounds: int = 8) -> None:
    root_pid = process.pid
    # Freeze the tree top-down before killing so no process can fork or exit
    # (reparenting grandchildren to PID 1) while descendants are collected.
    frozen: list[int] = []
    seen = {root_pid}
    _send_signal(root_pid, signal.SIGSTOP)
    for _ in range(max_rounds):
        added = False
        for pid in _descendants(root_pid):
            if pid not in seen:
                seen.add(pid)
                frozen.append(pid)
                _send_signal(pid, signal.SIGSTOP)
                added = True
        if not added:
            break
    for pid in reversed(frozen):
        _send_signal(pid, signal.SIGKILL)
    _send_signal(root_pid, signal.SIGKILL)
    for _ in range(max_rounds):
        remaining = _descendants(root_pid)
        if not remaining:
            break
        for pid in reversed(remaining):
            _send_signal(pid, signal.SIGKILL)
        _send_signal(root_pid, signal.SIGKILL)
        time.sleep(0.02)
    process.wait()


def supervise(
    process: subprocess.Popen[object], log: Path, *,
    batch_timeout: float, stall_seconds: float, suite_deadline: float | None,
    poll_interval: float = 0.2,
) -> tuple[int | None, str, bool]:
    """Wait for a batch process, enforcing suite deadline, batch timeout and silence stall."""
    started = time.monotonic()
    last_size = log.stat().st_size if log.is_file() else 0
    last_growth = started
    while True:
        exit_code = process.poll()
        if exit_code is not None:
            return exit_code, ("pass" if exit_code == 0 else "fail"), False
        now = time.monotonic()
        size = log.stat().st_size if log.is_file() else 0
        if size > last_size:
            last_size = size
            last_growth = now
        if suite_deadline is not None and now >= suite_deadline:
            kill_process_tree(process)
            return None, "timeout", True
        if now - started >= batch_timeout:
            kill_process_tree(process)
            return None, "timeout", False
        if now - last_growth >= stall_seconds:
            kill_process_tree(process)
            return None, "stalled", False
        deadlines = [started + batch_timeout, last_growth + stall_seconds]
        if suite_deadline is not None:
            deadlines.append(suite_deadline)
        wait_for = max(0.01, min(poll_interval, min(deadlines) - now))
        try:
            exit_code = process.wait(timeout=wait_for)
            return exit_code, ("pass" if exit_code == 0 else "fail"), False
        except subprocess.TimeoutExpired:
            continue


def last_log_line(log: Path) -> str:
    if not log.is_file():
        return "(no output)"
    lines = [line.rstrip() for line in log.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
    if not lines:
        return "(no output)"
    for line in reversed(lines):
        if ".py::" in line and not line[0].isspace():
            return line[:240]
    return lines[-1].strip()[:240]


def junit_test_count(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return 0
    return sum(1 for _ in root.iter("testcase"))
