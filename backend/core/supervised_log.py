"""Logging setup for the Python processes that supervisord starts."""
import logging
import multiprocessing
import os
import sys
import threading


class DatedFormatter(logging.Formatter):
    """Prefix every physical line, including traceback and multiline message continuations."""

    def __init__(self, formatter=logging.Formatter, fmt=None, include_name=False):
        super().__init__()
        self.inner = formatter(fmt=fmt)
        self.include_name = include_name

    def format(self, record):
        prefix = self.formatTime(record) + " "
        if self.include_name:
            prefix += f"[{record.name}] "
        return prefix + self.inner.format(record).replace("\n", "\n" + prefix)


def _uncaught(exc_type, exc, tb):
    logging.getLogger("uncaught").error("Uncaught exception", exc_info=(exc_type, exc, tb))


def _thread_error(args):
    if args.exc_type is not SystemExit:
        logging.getLogger("uncaught").error("Exception in thread %s", args.thread.name,
                                          exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


def _service_handler():
    handler = logging.StreamHandler()
    handler.setFormatter(DatedFormatter(include_name=True))
    sys.excepthook = _uncaught
    threading.excepthook = _thread_error
    return handler


def setup_logging():
    logging.basicConfig(level=logging.INFO, handlers=[_service_handler()], force=True)


def api_log_handler():
    """Uvicorn calls this factory in its main process and in each spawned worker."""
    handler = _service_handler()
    if os.environ.get("SUPERVISOR_PROCESS_NAME") == "backend" and multiprocessing.parent_process() is None:
        from core.config import warn_unused_settings

        # dictConfig has created the handler but has not yet attached it to the root.
        startup = logging.Logger("odin.api")
        startup.addHandler(handler)
        warn_unused_settings(startup)
    return handler
