"""Narrow, injectable subprocess boundary for lifecycle orchestration."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .errors import SandboxError


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class Executor:
    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        timeout: int = 300,
        input_bytes: bytes | None = None,
        check: bool = True,
    ) -> CommandResult:
        if not args or any(not isinstance(item, str) or not item for item in args):
            raise ValueError("command args must be non-empty strings")
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        completed = subprocess.run(
            list(args),
            cwd=cwd,
            env=merged_env,
            check=False,
            capture_output=True,
            input=input_bytes,
            timeout=timeout,
        )
        result = CommandResult(
            tuple(args),
            completed.returncode,
            completed.stdout.decode("utf-8", errors="replace"),
            completed.stderr.decode("utf-8", errors="replace"),
        )
        if check and result.returncode != 0:
            boundary = " ".join(args[:4])
            raise SandboxError(f"command failed ({result.returncode}): {boundary}")
        return result
