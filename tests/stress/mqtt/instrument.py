"""Opt-in instrumentation: monkeypatch `_on_status` to record per-call latency
and periodically sample DB row counts.

Activated by setting `ODIN_STRESS_INSTRUMENTATION=<output_dir>` in the
backend's environment before import. NEVER active in production — there's no
default.

Usage (from backend's startup, gated):

    if os.environ.get("ODIN_STRESS_INSTRUMENTATION"):
        from tests.stress.mqtt.instrument import install
        install(os.environ["ODIN_STRESS_INSTRUMENTATION"])

We monkeypatch instead of editing production code so the stress scaffold
stays self-contained and the production code path is byte-identical when
the env var is absent.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional


class LatencyRecorder:
    """Append-only JSONL writer for per-call timings. Thread-safe via lock."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fh = self.path.open("a", buffering=1)  # line-buffered
        self.count = 0

    def record(self, printer_id, fn_name: str, duration_s: float, ok: bool, err: Optional[str] = None) -> None:
        line = json.dumps({
            "ts": time.time(),
            "printer_id": printer_id,
            "fn": fn_name,
            "duration_ms": round(duration_s * 1000.0, 3),
            "ok": ok,
            "err": err,
        })
        with self._lock:
            self._fh.write(line + "\n")
            self.count += 1

    def close(self) -> None:
        with self._lock:
            try:
                self._fh.close()
            except Exception:
                pass


_recorder: Optional[LatencyRecorder] = None
_sampler_thread: Optional[threading.Thread] = None
_stop_sampler = threading.Event()


def install(output_dir: str) -> None:
    """Wrap `PrinterMonitor._on_status` and `BambuTelemetryAdapter._on_message`
    with timing envelopes. Spawn a background thread that samples row counts
    of `printer_telemetry` every 30s.

    Idempotent — calling twice is a no-op after the first install.
    """
    global _recorder, _sampler_thread
    if _recorder is not None:
        return

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    _recorder = LatencyRecorder(out / "latency.jsonl")
    samples_path = out / "samples.jsonl"

    # Stress-only: bypass community-tier 5-printer license cap so we can
    # bootstrap N=10/25/50/100 printers. Production code path is untouched
    # when the env var isn't set.
    try:
        import license_manager
        license_manager.check_printer_limit = lambda current_count: None
    except Exception as e:
        _recorder.record(None, "install.license_patch", 0.0, False, str(e))

    # Wrap legacy path
    try:
        from modules.printers.monitors import mqtt_printer
        _wrap(mqtt_printer.PrinterMonitor, "_on_status", _recorder)
    except Exception as e:
        _recorder.record(None, "install.legacy", 0.0, False, str(e))

    # Wrap V2 adapter
    try:
        from modules.printers.telemetry.bambu import adapter
        _wrap(adapter.BambuTelemetryAdapter, "_on_message", _recorder)
    except Exception as e:
        _recorder.record(None, "install.v2", 0.0, False, str(e))

    # DB row sampler
    db_path = os.environ.get("ODIN_DB_PATH") or "odin.db"
    _sampler_thread = threading.Thread(
        target=_sample_loop,
        args=(db_path, samples_path, 30.0),
        daemon=True,
    )
    _sampler_thread.start()


def _wrap(cls, method_name: str, recorder: LatencyRecorder) -> None:
    original = getattr(cls, method_name)

    def wrapped(self, *args, **kwargs):
        # Best-effort printer id extraction
        pid = getattr(self, "printer_id", None)
        if pid is None and hasattr(self, "_config"):
            pid = getattr(self._config, "printer_id", None)
        start = time.perf_counter()
        ok = True
        err = None
        try:
            return original(self, *args, **kwargs)
        except Exception as e:
            ok = False
            err = f"{type(e).__name__}: {str(e)[:200]}"
            raise
        finally:
            try:
                recorder.record(pid, f"{cls.__name__}.{method_name}", time.perf_counter() - start, ok, err)
            except Exception as _rec_err:
                # Last-ditch: write to stderr so we see why instrumentation
                # silently dropped a sample.
                import sys as _sys
                print(f"[stress-instr] record failed for {cls.__name__}.{method_name}: {_rec_err}", file=_sys.stderr)

    setattr(cls, method_name, wrapped)


def _sample_loop(db_path: str, out_path: Path, interval_s: float) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fh = out_path.open("a", buffering=1)
    try:
        while not _stop_sampler.wait(timeout=interval_s):
            sample = {"ts": time.time()}
            try:
                conn = sqlite3.connect(db_path, timeout=5)
                cur = conn.cursor()
                for table in ("printer_telemetry", "ams_telemetry", "alerts"):
                    try:
                        cur.execute(f"SELECT COUNT(*) FROM {table}")
                        sample[table] = cur.fetchone()[0]
                    except Exception as e:
                        sample[f"{table}_err"] = str(e)[:100]
                conn.close()
            except Exception as e:
                sample["sqlite_err"] = str(e)[:100]
            fh.write(json.dumps(sample) + "\n")
    finally:
        try:
            fh.close()
        except Exception:
            pass


def shutdown() -> None:
    """Flush + stop the sampler. Call on graceful shutdown if you can; the
    daemon thread will die with the process otherwise."""
    _stop_sampler.set()
    if _recorder is not None:
        _recorder.close()
