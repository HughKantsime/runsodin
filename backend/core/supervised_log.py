"""Timestamp complete lines from a supervised program, including native output and tracebacks."""
import os
import re
import sys
from datetime import datetime

_TIMESTAMPED = re.compile(rb"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \[")


def timestamp_lines(source, output, name):
    for line in source:
        if not _TIMESTAMPED.match(line):
            now = datetime.now().isoformat(sep=" ", timespec="milliseconds").replace(".", ",")
            line = f"{now} [{name}] ".encode() + line
        output.write(line)
        output.flush()


def launch(command):
    # Keep the program as supervisord's direct child: signals, exit codes and restart
    # policy still apply to it. The reader drains the pipe through EOF on exit.
    name = os.environ.get("SUPERVISOR_PROCESS_NAME", "odin")
    reader, writer = os.pipe()
    if os.fork() == 0:
        os.close(writer)
        with os.fdopen(reader, "rb") as source:
            timestamp_lines(source, sys.stdout.buffer, name)
        os._exit(0)
    os.close(reader)
    os.dup2(writer, 1)
    os.dup2(writer, 2)
    os.close(writer)
    os.execvp(command[0], command)


if __name__ == "__main__":
    launch(sys.argv[1:])
