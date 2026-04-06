# core/error_buffer.py — In-memory ring buffer for recent unhandled exceptions
#
# Stores the last 50 exceptions with truncated tracebacks. Lost on restart.
# No database tables — pure in-memory deque.

import traceback
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class ErrorEntry:
    timestamp: float
    exc_type: str
    exc_message: str
    traceback_frames: list[str]
    request_path: str | None = None
    request_method: str | None = None

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "exc_type": self.exc_type,
            "exc_message": self.exc_message,
            "traceback_frames": self.traceback_frames,
            "request_path": self.request_path,
            "request_method": self.request_method,
        }


class ErrorRingBuffer:
    def __init__(self, maxlen: int = 50):
        self._buffer: deque[ErrorEntry] = deque(maxlen=maxlen)

    def capture(self, exc: Exception, request=None) -> None:
        tb_lines = traceback.format_tb(exc.__traceback__) if exc.__traceback__ else []
        # Keep last 5 frames only
        tb_lines = tb_lines[-5:]

        entry = ErrorEntry(
            timestamp=time.time(),
            exc_type=type(exc).__name__,
            exc_message=str(exc),
            traceback_frames=tb_lines,
            request_path=str(request.url.path) if request else None,
            request_method=request.method if request else None,
        )
        self._buffer.append(entry)

    def entries(self) -> list[dict]:
        return [e.to_dict() for e in self._buffer]

    def clear(self) -> None:
        self._buffer.clear()


# Module-level singleton
error_buffer = ErrorRingBuffer()
