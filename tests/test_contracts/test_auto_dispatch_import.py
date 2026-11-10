"""Auto-dispatch on IDLE reaches the real dispatcher for every monitor.

Bambu (mqtt_printer) and PrusaLink used `import printer_dispatch`, a module that
does not exist; the ImportError was caught and logged, so AUTO_DISPATCH=true
never dispatched on those printers. Moonraker already used the right import.
"""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parents[2] / "backend"


def test_no_backend_module_imports_the_nonexistent_printer_dispatch_module():
    offenders = []
    for path in sorted(BACKEND.rglob("*.py")):
        if any(part.startswith("preserved") for part in path.parts):
            continue  # frozen historical bundles under core/schema, never imported at runtime
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import) and any(a.name == "printer_dispatch" for a in node.names):
                offenders.append(f"{path.relative_to(BACKEND)}:{node.lineno}")
            if isinstance(node, ast.ImportFrom) and node.module == "printer_dispatch":
                offenders.append(f"{path.relative_to(BACKEND)}:{node.lineno}")
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


def test_attempt_dispatch_is_a_no_op_with_auto_dispatch_off(monkeypatch):
    from modules.printers import dispatch
    monkeypatch.delenv("AUTO_DISPATCH", raising=False)
    looked_up = []
    monkeypatch.setattr(dispatch, "_get_next_scheduled_job", lambda *a, **k: looked_up.append(a) or None)
    monkeypatch.setattr(dispatch, "dispatch_job", lambda *a, **k: pytest.fail("dispatched with AUTO_DISPATCH off"))
    dispatch.attempt_dispatch(42)
    assert looked_up == []
