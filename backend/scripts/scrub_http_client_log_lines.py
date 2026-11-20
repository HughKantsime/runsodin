"""Clean old HTTP request lines once, before Supervisor opens the logs."""
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

from core.log_redaction import redact_text


def _redact(line: bytes) -> bytes:
    if b"HTTP Request: " not in line:
        return line
    return redact_text(line.decode("utf-8", "surrogateescape")).encode("utf-8", "surrogateescape")


def _open_log(path, flags):
    return os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK)


def _scrub_file(path: Path) -> int:
    with open(path, "rb", opener=_open_log) as source:
        mode = os.fstat(source.fileno()).st_mode
        if not stat.S_ISREG(mode) or not any(_redact(line) != line for line in source):
            return 0

        source.seek(0)
        changed = 0
        tmp = tempfile.NamedTemporaryFile(dir=path.parent, delete=False)
        try:
            with tmp:
                for line in source:
                    clean = _redact(line)
                    tmp.write(clean)
                    changed += clean != line
                tmp.flush()
                os.fchmod(tmp.fileno(), stat.S_IMODE(mode))
                os.fsync(tmp.fileno())
            os.replace(tmp.name, path)
        finally:
            Path(tmp.name).unlink(missing_ok=True)
        return changed


def main(data: Path) -> None:
    marker = data / ".http_client_log_scrub_done"
    examined = rewritten = redacted = 0
    failed = False
    target = data
    try:
        if os.path.lexists(marker):
            return
        for path in data.iterdir():
            if not re.search(r"\.log(?:\.[0-9]+)?\Z", path.name):
                continue
            try:
                if not stat.S_ISREG(path.lstat().st_mode):
                    continue
                examined += 1
                changed = _scrub_file(path)
                if changed:
                    rewritten += 1
                    redacted += changed
            except Exception as exc:
                print(f"WARNING: log scrub failed for {path.name}: {type(exc).__name__}", file=sys.stderr)
                failed = True
        if not failed:
            target = marker
            with marker.open("xb"):
                pass
    except Exception as exc:
        print(f"WARNING: log scrub failed for {target.name}: {type(exc).__name__}", file=sys.stderr)
    finally:
        print(f"Log cleanup: {examined} files examined, {rewritten} files rewritten, {redacted} lines redacted.")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/data"))
