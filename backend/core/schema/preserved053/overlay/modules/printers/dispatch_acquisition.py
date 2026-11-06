"""Owned-process bounds for fresh printer readiness acquisition.

Import-safe: no database, subscribers or device actions at module import.
Credentials travel only in memory. A completed read is accepted only after
normal worker exit before the admission deadline. Forced cleanup always
refuses readiness and gets a separate two-second termination allowance.
"""
import math
import multiprocessing
import threading
import time


def _http_worker(sender, creds, deadline):
    try:
        from modules.printers.dispatch_readiness import fresh_http_status_is_idle
        result = fresh_http_status_is_idle(creds, deadline)
        sender.send(result is True)
    except Exception:
        try:
            sender.send(False)
        except Exception:
            pass
    finally:
        sender.close()


def _bambu_worker(sender, creds, deadline):
    try:
        from modules.printers.dispatch_bambu_readiness import fresh_bambu_status_is_idle
        sender.send(fresh_bambu_status_is_idle(creds,deadline) is True)
    except Exception:
        try:sender.send(False)
        except Exception:pass
    finally:sender.close()


def _bambu_report_worker(sender, creds, deadline):
    try:
        from modules.printers.dispatch_bambu_readiness import fresh_bambu_status_is_idle
        sender.send(fresh_bambu_status_is_idle(creds, deadline, return_report=True))
    except Exception:
        try:sender.send(None)
        except Exception:pass
    finally:sender.close()


def bounded_bambu_report(creds, deadline):
    result = bounded_status_is_idle(creds, deadline, _report=True)
    return result if isinstance(result, dict) else None


def _spawn_context():
    return multiprocessing.get_context('spawn')


def _cleanup_owned_process(process):
    clean = True
    try:
        alive = process.is_alive()
    except Exception:
        clean, alive = False, True
    if alive:
        clean = False  # Forced retirement never establishes readiness.
        try:
            process.terminate()
        except Exception:
            pass
        try:
            process.join(timeout=1)
        except Exception:
            pass
        try:
            alive = process.is_alive()
        except Exception:
            alive = True
        if alive:
            try:
                process.kill()
            except Exception:
                pass
            try:
                process.join(timeout=1)
            except Exception:
                pass
    try:
        if process.is_alive():
            return False
    except Exception:
        clean = False
    try:
        process.close()
    except Exception:
        clean = False
    return clean


def bounded_http_status_is_idle(creds: dict, deadline: float) -> bool:
    if not isinstance(creds,dict) or creds.get('api_type') not in ('moonraker','prusalink'):
        return False
    return bounded_status_is_idle(creds,deadline)


def bounded_status_is_idle(creds: dict, deadline: float, *, _report=False):
    """Refuse late/hanging reads; only reap the process created by this call."""
    receiver = sender = process = None
    accepted = False
    try:
        if not isinstance(creds, dict) or creds.get('api_type') not in ('bambu','moonraker', 'prusalink'):
            return False
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
            return False
        deadline = float(deadline)
        remaining = deadline - time.monotonic()
        if not math.isfinite(deadline) or not 0 < remaining <= threading.TIMEOUT_MAX:
            return False
        context = _spawn_context()
        receiver, sender = context.Pipe(duplex=False)
        # Whitelist only acquisition inputs, not the complete printer/job record.
        keys = ('api_type','ip','serial','access_code','machine_type','model') if creds['api_type']=='bambu' else ('api_type','ip','port','scheme','api_key','username','password')
        inputs = {key:creds[key] for key in keys if key in creds}
        worker = _bambu_worker if creds['api_type'] == 'bambu' else _http_worker
        if _report:
            if creds['api_type'] != 'bambu':return False
            worker = _bambu_report_worker
        process = context.Process(target=worker, args=(sender, inputs, deadline), daemon=True)
        process.start()
        sender.close()
        remaining = deadline - time.monotonic()
        if remaining > 0 and receiver.poll(remaining):
            result = receiver.recv()
            remaining = deadline - time.monotonic()
            if remaining > 0:
                process.join(timeout=remaining)
                valid = isinstance(result, dict) if _report else type(result) is bool and result
                if valid and process.exitcode == 0 and not process.is_alive() and time.monotonic() < deadline:
                    accepted = result
    except Exception:
        accepted = False
    finally:
        for end in (receiver, sender):
            if end is not None:
                try:
                    end.close()
                except Exception:
                    accepted = False
        if process is not None:
            if not _cleanup_owned_process(process):
                accepted = False
    return accepted
