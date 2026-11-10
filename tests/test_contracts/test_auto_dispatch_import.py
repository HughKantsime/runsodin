"""Auto-dispatch on IDLE reaches the real dispatcher for every monitor.

Bambu (mqtt_printer) and PrusaLink used `import printer_dispatch`, a module that
does not exist; the ImportError was caught and logged, so AUTO_DISPATCH=true
never dispatched on those printers. Moonraker already used the right import.
"""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

MONITORS = Path(__file__).resolve().parents[2] / "backend" / "modules" / "printers" / "monitors"


def test_no_monitor_imports_the_nonexistent_printer_dispatch_module():
    offenders = []
    for path in sorted(MONITORS.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import) and any(a.name == "printer_dispatch" for a in node.names):
                offenders.append(f"{path.name}:{node.lineno}")
            if isinstance(node, ast.ImportFrom) and node.module == "printer_dispatch":
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, offenders


@pytest.mark.parametrize("module_name, cls_name", [
    ("modules.printers.monitors.mqtt_printer", "PrinterMonitor"),
    ("modules.printers.monitors.prusalink_monitor", "PrusaLinkMonitorThread"),
])
def test_try_dispatch_calls_attempt_dispatch(monkeypatch, module_name, cls_name):
    import importlib
    module = importlib.import_module(module_name)
    from modules.printers import dispatch
    calls = []
    monkeypatch.setattr(dispatch, "attempt_dispatch", lambda printer_id: calls.append(printer_id))
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    warnings = []
    monkeypatch.setattr(module.log, "warning", lambda *a, **k: warnings.append(a))
    getattr(module, cls_name)._try_dispatch(SimpleNamespace(printer_id=42, name="test-printer"))
    assert calls == [42]
    assert not warnings
