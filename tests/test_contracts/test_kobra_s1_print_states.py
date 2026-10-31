"""A print closes from every state an S1 can abort in, whichever view of its state ODIN gets.

Moonraker's subscription reports printing, paused and a relative file name through prep and pause
(swap capture). The raw gkapi stream reports leveling, heating, OnPause and an absolute name. Whether
a one-shot query on a real S1 can show the raw view isn't proven, so both must behave.
"""
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from modules.printers.kobra_s1 import S1Refusal
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_kobra_s1_ace_transient import FRAMES, GK_ROOT, IDLE_T, _frame_at, s1, slot_rows
from tests.test_contracts.test_kobra_s1_release1 import S1_HEADER


def _started(s1, channel='gk'):
    s1.device.channel = channel
    s1.device.frame = _frame_at(IDLE_T)
    s1.monitor.observe()
    result = s1.start(53)
    assert result[0] is True, result


def _facts(engine):
    with engine.connect() as c:
        return SimpleNamespace(
            job=c.execute(text('SELECT status FROM jobs WHERE id=53')).scalar_one(),
            rows=c.execute(text('SELECT scheduled_job_id,status FROM print_jobs')).all(),
            claims=c.execute(text('SELECT COUNT(*) FROM printer_dispatch_claims')).scalar_one())


@pytest.mark.parametrize('channel,states,job_status', [
    pytest.param('gk', ['leveling', 'heating', 'cancelled'], 'cancelled', id='gk_cancelled_during_prep'),
    pytest.param('gk', ['heating', 'cancelled'], 'cancelled', id='gk_first_seen_heating_then_cancelled'),
    pytest.param('gk', ['heating', 'error'], 'failed', id='gk_error_during_prep'),
    pytest.param('gk', ['leveling', 'standby'], 'cancelled', id='gk_prep_abort_lands_on_standby'),
    pytest.param('gk', ['OnPause', 'cancelled'], 'cancelled', id='gk_first_seen_in_onpause_then_cancelled'),
    pytest.param('gk', ['printing', 'OnPause', 'cancelled'], 'cancelled', id='gk_cancelled_during_onpause_never_polled_paused'),
    pytest.param('gk', ['printing', 'OnPause', 'error'], 'failed', id='gk_error_during_onpause'),
    pytest.param('gk', ['printing', 'OnPause', 'paused', 'cancelled'], 'cancelled', id='gk_cancelled_after_paused'),
    pytest.param('gk', ['heating', 'stopping', 'cancelled'], 'cancelled', id='gk_a_state_nothing_here_knows'),
    pytest.param('moon', ['printing', 'cancelled'], 'cancelled', id='moon_cancelled_during_prep'),
    pytest.param('moon', ['printing', 'error'], 'failed', id='moon_error_during_prep'),
    pytest.param('moon', ['printing', 'paused', 'cancelled'], 'cancelled', id='moon_cancelled_after_paused'),
    pytest.param('moon', ['printing', 'stopping', 'cancelled'], 'cancelled', id='moon_a_state_nothing_here_knows'),
])
def test_a_print_aborted_in_any_state_frees_the_printer(s1, channel, states, job_status):
    _started(s1, channel)
    for state in states:
        s1.device.state = state
        s1.monitor.observe()
    facts = _facts(s1.engine)
    assert facts.job == job_status
    assert [tuple(r) for r in facts.rows] == [(53, job_status)]
    assert facts.claims == 0
    # Real evidence the printer isn't stranded: the next start gets through.
    s1.device.state = 'standby'
    s1.monitor.observe()
    result = s1.start(54)
    assert result[0] is True, result


@pytest.mark.parametrize('channel,seen,after,job_status', [
    pytest.param('gk', 'heating', ['heating', 'cancelled'], 'cancelled', id='gk_heating_then_cancelled'),
    pytest.param('gk', 'heating', ['heating', 'error'], 'failed', id='gk_heating_then_error'),
    pytest.param('gk', 'leveling', ['heating', 'printing', 'OnPause', 'cancelled'], 'cancelled', id='gk_prep_to_onpause_then_cancelled'),
    pytest.param('gk', 'printing', ['OnPause', 'cancelled'], 'cancelled', id='gk_first_look_after_restart_is_onpause'),
    pytest.param('moon', 'printing', ['printing', 'cancelled'], 'cancelled', id='moon_printing_then_cancelled'),
])
def test_a_monitor_restart_during_prep_still_closes_the_print_once(s1, channel, seen, after, job_status):
    from modules.printers.monitors import moonraker_monitor
    _started(s1, channel)
    s1.device.state = seen
    s1.monitor.observe()
    assert s1.monitor._current_job_db_id
    monitor = moonraker_monitor.MoonrakerMonitor(59, 'Synthetic', '127.0.0.1', model='Kobra S1')
    for state in after:
        s1.device.state = state
        monitor._process_status(monitor.printer.get_status())
    facts = _facts(s1.engine)
    assert facts.job == job_status
    assert [tuple(r) for r in facts.rows] == [(53, job_status)]
    assert facts.claims == 0


def test_a_finished_print_passing_through_standby_still_completes(s1):
    """gkapi goes printing, standby, complete at the end of a normal print (t=1470.5 in the capture)."""
    _started(s1)
    s1.device.state = 'printing'
    s1.monitor.observe()
    s1.device.state, s1.device.print_duration = 'standby', 803
    s1.monitor.observe()
    assert _facts(s1.engine).job == 'completed'


def test_a_print_reporting_complete_with_no_print_time_still_completes(s1):
    """The no-print-time rule is for landing on standby; complete is the printer's own word."""
    _started(s1)
    s1.device.state = 'printing'
    s1.monitor.observe()
    s1.device.state, s1.device.print_duration = 'complete', 0
    s1.monitor.observe()
    assert _facts(s1.engine).job == 'completed'


def test_onpause_records_the_pause_and_the_resume(s1, monkeypatch):
    from core import job_timeline
    kinds = []
    monkeypatch.setattr(job_timeline, 'for_printer', lambda printer_id, kind, **details: kinds.append(kind))
    _started(s1)
    for state in ('printing', 'OnPause', 'printing'):
        s1.device.state = state
        s1.monitor.observe()
    assert kinds == ['paused', 'resumed']


def _iso(moment):
    return datetime.fromtimestamp(moment, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')


def _utc(value):
    return datetime.fromisoformat(str(value)).replace(tzinfo=timezone.utc).timestamp()


def test_a_paused_print_keeps_its_start_when_the_s1s_history_splits_at_the_pause(s1):
    """The swap capture: a record ends cancelled at the pause, and the one that finishes starts at the resume."""
    _started(s1, 'moon')
    s1.device.state = 'printing'
    s1.monitor.observe()
    now = time.time()
    start, resume, end = now - 1403, now - 772, now - 5
    with s1.engine.begin() as c:
        c.execute(text('UPDATE print_jobs SET started_at=:at WHERE scheduled_job_id=53'), {'at': _iso(start)})
        c.execute(text('UPDATE jobs SET actual_start=:at WHERE id=53'), {'at': _iso(start)})
    for state in ('paused', 'printing'):
        s1.device.state = state
        s1.monitor.observe()
    name = s1.device.filename
    s1.device.jobs = [dict(filename=name, status='completed', start_time=resume, end_time=end, print_duration=803, total_duration=767),
                      dict(filename=name, status='cancelled', start_time=start, end_time=resume, print_duration=31, total_duration=631)]
    s1.device.state, s1.device.print_duration = 'complete', 803
    s1.monitor.observe()
    with s1.engine.connect() as c:
        row = c.execute(text('SELECT status,started_at,ended_at FROM print_jobs WHERE scheduled_job_id=53')).one()
        job_start = c.execute(text('SELECT actual_start FROM jobs WHERE id=53')).scalar_one()
    assert row[0] == 'completed'
    assert _utc(row[1]) == pytest.approx(start, abs=1), 'The row starts when the print did, not at the resume'
    assert datetime.fromisoformat(str(row[2])).timestamp() == pytest.approx(end, abs=1)
    assert _utc(job_start) == pytest.approx(start, abs=1)


@pytest.mark.parametrize('states', [
    pytest.param(['leveling', 'heating', 'printing'], id='gk_prep_to_print'),
    pytest.param(['leveling', 'heating', 'printing', 'OnPause', 'paused', 'printing'], id='gk_prep_print_pause_resume'),
])
def test_prep_to_print_binds_exactly_once(s1, states):
    _started(s1)
    bound = None
    for state in states:
        s1.device.state = state
        s1.monitor.observe()
        bound = bound or s1.monitor._current_job_db_id
        assert s1.monitor._current_job_db_id == bound and bound
    facts = _facts(s1.engine)
    assert facts.job == 'printing' and [tuple(r) for r in facts.rows] == [(53, 'running')]
    with s1.engine.connect() as c:
        assert c.execute(text('SELECT observed_print_job_id FROM dispatch_attempt_receipts WHERE job_id=53')).scalar_one() == bound


@pytest.mark.parametrize('raw,expected', [
    (GK_ROOT + 'odin-dispatch-x.gcode', 'odin-dispatch-x.gcode'),
    ('odin-dispatch-x.gcode', 'odin-dispatch-x.gcode'),
    ('', ''),
    ('/other/root/x.gcode', '/other/root/x.gcode'),
])
def test_an_s1_reports_the_name_moonraker_uses(s1, raw, expected):
    s1.device.channel = 'moon'
    s1.device.filename = raw
    assert s1.monitor.printer.get_status().filename == expected


@pytest.mark.parametrize('state', ['leveling', 'heating', 'OnPause', 'printing', 'paused', 'stopping'])
def test_dispatch_still_refuses_while_the_s1_is_busy(s1, state):
    from modules.printers.s1_dispatch import _fresh
    s1.device.frame = _frame_at(IDLE_T)
    s1.device.state = state
    with pytest.raises(S1Refusal, match='s1_printer_not_ready'):
        _fresh(s1.monitor.printer)


def test_raw_state_stays_raw_so_only_internal_state_changes(s1):
    s1.device.frame = _frame_at(IDLE_T)
    s1.device.state = 'heating'
    status = s1.monitor.printer.get_status()
    assert status.internal_state == 'RUNNING' and status.raw_print_state == 'unknown'
    assert status.raw_data['print_stats']['state'] == 'heating'


def test_other_moonraker_printers_are_unchanged(review_db, tmp_path, monkeypatch):
    from modules.printers.adapters import moonraker
    from modules.printers.monitors import moonraker_monitor
    from tests.test_contracts.test_dispatch_real_monitor_correlation import configure_dispatch
    engine, _ = configure_dispatch(review_db, tmp_path, monkeypatch)
    device = SimpleNamespace(state='standby', filename=GK_ROOT + 'x.gcode')
    def query(self, path, timeout=5):
        if path.startswith('/server/history'):
            return {'result': {'jobs': []}}
        return {'result': {'status': dict(print_stats=dict(state=device.state, filename=device.filename, print_duration=0), webhooks=dict(state='ready'))}}
    monkeypatch.setattr(moonraker.MoonrakerPrinter, '_get', query)
    monkeypatch.setattr(moonraker_monitor, 'engine', engine)
    monitor = moonraker_monitor.MoonrakerMonitor(59, 'Synthetic', '127.0.0.1', model='Voron 2.4')
    observe = lambda: monitor._process_status(monitor.printer.get_status())
    for state, internal in (('leveling', 'UNKNOWN'), ('heating', 'UNKNOWN'), ('OnPause', 'UNKNOWN'), ('printing', 'RUNNING')):
        device.state = state
        status = monitor.printer.get_status()
        assert status.internal_state == internal and status.filename == GK_ROOT + 'x.gcode'
    observe()
    tracked = monitor._current_job_db_id
    assert tracked
    device.state = 'OnPause'
    observe()
    device.state = 'cancelled'
    observe()
    with engine.connect() as c:
        assert c.execute(text('SELECT status FROM print_jobs WHERE id=:id'), {'id': tracked}).scalar_one() == 'running'
    assert monitor._current_job_db_id == tracked


def test_standby_ends_another_moonraker_printers_print_as_before(review_db, tmp_path, monkeypatch):
    from modules.printers.adapters import moonraker
    from modules.printers.monitors import moonraker_monitor
    from tests.test_contracts.test_dispatch_real_monitor_correlation import configure_dispatch
    engine, _ = configure_dispatch(review_db, tmp_path, monkeypatch)
    device = SimpleNamespace(state='printing')
    def query(self, path, timeout=5):
        if path.startswith('/server/history'):
            return {'result': {'jobs': []}}
        return {'result': {'status': dict(print_stats=dict(state=device.state, filename='x.gcode', print_duration=0), webhooks=dict(state='ready'))}}
    monkeypatch.setattr(moonraker.MoonrakerPrinter, '_get', query)
    monkeypatch.setattr(moonraker_monitor, 'engine', engine)
    monitor = moonraker_monitor.MoonrakerMonitor(59, 'Synthetic', '127.0.0.1', model='Voron 2.4')
    monitor._process_status(monitor.printer.get_status())
    tracked = monitor._current_job_db_id
    device.state = 'standby'
    monitor._process_status(monitor.printer.get_status())
    with engine.connect() as c:
        assert c.execute(text('SELECT status FROM print_jobs WHERE id=:id'), {'id': tracked}).scalar_one() == 'completed'


def test_another_moonraker_printers_row_still_takes_a_later_history_start(review_db, tmp_path, monkeypatch):
    from modules.printers.adapters import moonraker
    from modules.printers.monitors import moonraker_monitor
    from tests.test_contracts.test_dispatch_real_monitor_correlation import configure_dispatch
    engine, _ = configure_dispatch(review_db, tmp_path, monkeypatch)
    device = SimpleNamespace(state='printing', jobs=[])
    def query(self, path, timeout=5):
        if path.startswith('/server/history'):
            return {'result': {'jobs': device.jobs}}
        return {'result': {'status': dict(print_stats=dict(state=device.state, filename='x.gcode', print_duration=60), webhooks=dict(state='ready'))}}
    monkeypatch.setattr(moonraker.MoonrakerPrinter, '_get', query)
    monkeypatch.setattr(moonraker_monitor, 'engine', engine)
    monitor = moonraker_monitor.MoonrakerMonitor(59, 'Synthetic', '127.0.0.1', model='Voron 2.4')
    monitor._process_status(monitor.printer.get_status())
    tracked = monitor._current_job_db_id
    now = time.time()
    with engine.begin() as c:
        c.execute(text('UPDATE print_jobs SET started_at=:at WHERE id=:id'), {'at': _iso(now - 1000), 'id': tracked})
    device.jobs = [dict(filename='x.gcode', status='completed', start_time=now - 100, end_time=now - 5, print_duration=95, total_duration=95)]
    device.state = 'complete'
    monitor._process_status(monitor.printer.get_status())
    with engine.connect() as c:
        started = c.execute(text('SELECT started_at FROM print_jobs WHERE id=:id'), {'id': tracked}).scalar_one()
    assert datetime.fromisoformat(str(started)).timestamp() == pytest.approx(now - 100, abs=1)
