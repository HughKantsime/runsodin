"""Alert staging and mobile delivery work in a monitor daemon's import set.

The MQTT monitor process never imports the Printer model, so the first ORM use
of Alert or PushDevice failed mapper configuration and every pause alert and
daemon-side mobile push was dropped (prod 2026-10-29, P1S job 110).
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

SCENARIO = '''
import sys
import modules.printers.monitors.mqtt_monitor  # noqa: F401  the daemon's own import set
from sqlalchemy.orm import configure_mappers
from core.base import AlertType, AlertSeverity
entry = sys.argv[1]
if entry == "stage_alert":
    from modules.notifications.alert_dispatcher import stage_alert
    try:
        stage_alert(None, AlertType.PRINT_PAUSED, AlertSeverity.CRITICAL, "Print needs attention", "Print paused", printer_id=1)
    except Exception:
        pass  # No session here; only the registry guard matters.
else:
    import modules.notifications.models  # noqa: F401  any earlier alert path imports Alert
    from modules.notifications.alert_outbox import deliver_mobile
    deliver_mobile(None, None)
configure_mappers()
print("ORM_REGISTRY_OK")
'''


@pytest.mark.parametrize('entry', ['stage_alert', 'deliver_mobile'])
def test_alert_paths_configure_every_mapper_in_monitor_process(tmp_path, entry):
    env = dict(os.environ, DATABASE_URL='sqlite:///' + str(tmp_path / 'app.db'), LICENSE_DIR=str(tmp_path),
               API_KEY='', ODIN_ITAR_MODE='0', ODIN_TELEMETRY_V2='0', COOKIE_SECURE='false',
               PYTHONPATH=f"{ROOT / 'backend'}:{ROOT}")
    result = subprocess.run([sys.executable, '-c', SCENARIO, entry], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-4000:]
    assert 'ORM_REGISTRY_OK' in result.stdout


def _daemon_modules():
    import re
    names = set()
    for conf in sorted((ROOT / 'docker').glob('supervisord*.conf')):
        names.update(re.findall(r'^command=python3 -m (modules\.[\w.]+)\s*$', conf.read_text(), re.M))
    return sorted(names)


DAEMONS = _daemon_modules()


def test_daemon_inventory_is_complete():
    assert len(DAEMONS) == 8 and 'modules.printers.monitors.mqtt_monitor' in DAEMONS


@pytest.mark.parametrize('module', DAEMONS)
def test_every_daemon_configures_orm_at_startup(module):
    source = (ROOT / 'backend' / (module.replace('.', '/') + '.py')).read_text()
    main = source[source.rindex("__main__"):]
    assert main.index('configure_orm()') < min(
        i for i in (main.find(word) for word in ('main()', 'main_loop()', 'start(', '.run()', 'while ')) if i >= 0)


ORDER_SCENARIO = '''
import importlib, sys
importlib.import_module(sys.argv[1])  # the daemon's own import set, without __main__
from core.schema.bootstrap import configure_orm
configure_orm()
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from core.base import Base, AlertType, AlertSeverity
engine = create_engine("sqlite://")
Base.metadata.create_all(engine)
from modules.jobs.models import Job
from modules.notifications.models import Alert
from modules.push.models import PushDevice
with Session(engine) as db:
    assert db.get(Job, 1) is None  # completion summary's ORM lookup comes first in prod
    db.add(Alert(user_id=1, alert_type=AlertType.PRINT_PAUSED, severity=AlertSeverity.CRITICAL,
                 title="Print paused", message="AMS: Filament ran out."))
    db.commit()
    assert db.query(Alert).count() == 1 and db.query(PushDevice).all() == []
print("ORM_ORDER_OK")
'''


@pytest.mark.parametrize('module', DAEMONS)
def test_daemon_import_set_persists_alerts_after_summary_lookup(tmp_path, module):
    env = dict(os.environ, DATABASE_URL='sqlite:///' + str(tmp_path / 'app.db'), LICENSE_DIR=str(tmp_path),
               API_KEY='', ODIN_ITAR_MODE='0', ODIN_TELEMETRY_V2='0', COOKIE_SECURE='false',
               PYTHONPATH=f"{ROOT / 'backend'}:{ROOT}")
    result = subprocess.run([sys.executable, '-c', ORDER_SCENARIO, module], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-4000:]
    assert 'ORM_ORDER_OK' in result.stdout
