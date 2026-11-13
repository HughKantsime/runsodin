"""A fed ACE slot moves during a print. That motion must not read as an unloaded slot."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from modules.printers.kobra_s1 import S1Refusal, parse_ace, verify_mapping
from tests.test_contracts.test_dispatch_real_monitor_correlation import configure_dispatch
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_kobra_s1_release1 import S1_HEADER, _bed_clear_request

FIXTURES = Path(__file__).resolve().parents[2] / 'backend/tests/fixtures/kobra_s1'
FRAMES = json.loads((FIXTURES / 'ace_status_sequence.json').read_text())['frames']
FILAMENT = parse_ace(dict(filament_hubs=[dict(slots=FRAMES[0]['slots'])]))['slots'][0]
GK_ROOT = '/useremain/app/gk/gcodes/'
IDLE_T = 68.8  # Moonraker and gkapi both say standby, just before the capture's print starts.


def hub(frame):
    return dict(auto_refill=frame['auto_refill'], current_filament=frame['current_filament'],
                filament_hubs=[dict(status='ready', slots=frame['slots'])])


def _frame_at(t):
    return [frame for frame in FRAMES if frame['t'] <= t][-1]


@pytest.fixture
def s1(review_db, tmp_path, monkeypatch):
    """Printer 59 as an S1, fed from slot 1 like the capture. Slot 4 carries a feed restriction."""
    from modules.printers import dispatch, start_next
    from modules.printers.adapters import anycubic_lan, moonraker
    from modules.printers.monitors import moonraker_monitor
    engine, path = configure_dispatch(review_db, tmp_path, monkeypatch)
    path.write_text(S1_HEADER + '; filament_colour = #00AE42\n; filament_type = PLA\nG28\n')
    with engine.begin() as c:
        c.execute(text("UPDATE printers SET model='Kobra S1',slot_count=4 WHERE id=59"))
        c.execute(text('DELETE FROM filament_slots WHERE printer_id=59'))
        c.execute(text("UPDATE jobs SET ams_mapping='[0]' WHERE id IN (53,54)"))
    device = SimpleNamespace(frame=FRAMES[0], filename='', publishes=[], channel='moon', state=None, print_duration=0, jobs=[])
    def query(self, path, timeout=5):
        if path.startswith('/server/history'):
            return {'result': {'jobs': device.jobs}}
        if getattr(device, 'confirming', False):
            device.confirming = False
            return {'result': {'status': dict(print_stats=dict(state='printing', filename=device.filename), extruder=dict(target=210), webhooks=dict(state='ready'), filament_hub=hub(device.frame))}}
        # What Moonraker reports, or the raw gkapi view of the same moment.
        state = device.state or device.frame['moon_state' if device.channel == 'moon' else 'gk_state']
        filename = device.filename if device.channel == 'moon' else GK_ROOT + device.filename
        return {'result': {'status': dict(
            print_stats=dict(state=state, filename=filename, filament_used=0, print_duration=device.print_duration),
            extruder=dict(target=getattr(device, 'nozzle_target', 210 if state.lower() in ('printing', 'leveling', 'heating', 'paused', 'onpause') else 0)),
            webhooks=dict(state='ready'), filament_hub=hub(device.frame))}}
    def upload(self, local, remote):
        device.filename = remote
        return True
    def send(self, payload, *, before_publish):
        before_publish()
        device.publishes.append(payload)
        device.confirming = True
        return dict(published=True, message='Published; no correlated report. Do not retry automatically.')
    monkeypatch.setattr(moonraker.MoonrakerPrinter, '_get', query)
    monkeypatch.setattr(moonraker.MoonrakerPrinter, 'upload_file', upload)
    monkeypatch.setattr(moonraker.MoonrakerPrinter, 'device_file_identity',
                        lambda self, name: dict(path=name, md5='b' * 32, size=999, filaments=[dict(color='#00AE42', type='PLA')]))
    monkeypatch.setattr(anycubic_lan.AnycubicLANPrinter, 'send_start', send)
    monkeypatch.setattr(moonraker_monitor, 'engine', engine)
    monitor = moonraker_monitor.MoonrakerMonitor(59, 'Synthetic', '127.0.0.1', model='Kobra S1')
    monitor.observe = lambda: monitor._process_status(monitor.printer.get_status())
    def start(job):
        marker = _bed_clear_request(engine, job, 59)
        try:
            return dispatch.dispatch_job(59, job)
        finally:
            start_next._context.reset(marker)
    return SimpleNamespace(engine=engine, device=device, monitor=monitor, start=start)


def slot_rows(engine):
    with engine.connect() as c:
        return {r.slot_number: r for r in c.execute(text(
            'SELECT slot_number,filament_type,color_hex,do_not_feed,do_not_feed_note FROM filament_slots WHERE printer_id=59')).all()}


@pytest.mark.parametrize('channel', ['moon', 'gk'])
@pytest.mark.parametrize('first_poll,fed_status', [
    pytest.param(70.5, 'ready', id='first_poll_early_in_prep'),
    pytest.param(602.5, 'unwinding', id='first_poll_while_the_fed_slot_unwinds'),
    pytest.param(604.5, 'ready', id='first_poll_after_the_fed_slot_settles'),
])
def test_start_binds_once_and_completes_through_the_captured_sequence(s1, channel, first_poll, fed_status):
    engine, device, monitor = s1.engine, s1.device, s1.monitor
    device.channel = channel
    # Idle: the monitor creates the slots from a settled report. Then one is restricted.
    device.frame = _frame_at(IDLE_T)
    monitor.observe()
    with engine.begin() as c:
        c.execute(text("UPDATE filament_slots SET do_not_feed=TRUE,do_not_feed_note='Feed fault' WHERE printer_id=59 AND slot_number=4"))
    result = s1.start(53)
    assert result[0] is True, result
    t = first_poll
    bound = None
    while t <= FRAMES[-1]['t'] + 3:
        device.frame = _frame_at(t)
        device.print_duration = max(0, t - 602)
        monitor.observe()
        rows = slot_rows(engine)
        assert rows[1].filament_type == 'PLA', (t, device.frame['slots'][0]['status'])
        assert rows[1].color_hex == FILAMENT['color_hex']
        assert bool(rows[4].do_not_feed) is True and rows[4].do_not_feed_note == 'Feed fault'
        if bound is None:
            bound = monitor._current_job_db_id
            assert bound and device.frame['slots'][0]['status'] == fed_status
            with engine.connect() as c:
                observed = c.execute(text('SELECT observed_print_job_id FROM dispatch_attempt_receipts WHERE job_id=53')).scalar_one()
                linked = c.execute(text('SELECT scheduled_job_id FROM print_jobs WHERE id=:id'), {'id': bound}).scalar_one()
            assert observed == bound and linked == 53
        t += 3
    with engine.connect() as c:
        assert c.execute(text('SELECT status FROM jobs WHERE id=53')).scalar_one() == 'completed'
        assert c.execute(text('SELECT COUNT(*) FROM printer_dispatch_claims')).scalar_one() == 0
        assert c.execute(text('SELECT COUNT(*) FROM print_jobs')).scalar_one() == 1, 'One print, one row, through prep and the pause'
    # The next attempt is not fenced by a claim the first one stranded.
    device.frame = _frame_at(FRAMES[-1]['t'])
    result = s1.start(54)
    assert result[0] is True, result
    assert len(device.publishes) == 2


@pytest.mark.parametrize('status', ['shifting', 'unwinding', 'feeding', 'some_future_status', None])
def test_a_slot_in_motion_keeps_its_material_and_stays_unusable(s1, status):
    from modules.printers.monitors.ams_inventory import sync_ace_inventory
    settled = json.loads(json.dumps(FRAMES[0]))
    moving = json.loads(json.dumps(FRAMES[0]))
    moving['slots'][0]['status'] = status
    with s1.engine.begin() as c:
        sync_ace_inventory(c, 59, parse_ace(hub(settled)))
        c.execute(text("UPDATE filament_slots SET do_not_feed=TRUE,do_not_feed_note='Feed fault' WHERE printer_id=59 AND slot_number=1"))
    before = slot_rows(s1.engine)[1]
    with s1.engine.begin() as c:
        sync_ace_inventory(c, 59, parse_ace(hub(moving)))
    assert slot_rows(s1.engine)[1] == before
    assert before.filament_type == 'PLA' and bool(before.do_not_feed) is True
    with pytest.raises(S1Refusal, match='s1_slot_unusable'):
        verify_mapping([dict(color='#00AE42', type='PLA')], parse_ace(hub(moving))['slots'], [0])


def test_a_slot_in_motion_changes_nothing_even_if_its_report_differs(s1):
    """Keeping the last material means the row, not a re-read of the moving slot's type."""
    from modules.printers.monitors.ams_inventory import sync_ace_inventory
    settled = json.loads(json.dumps(FRAMES[0]))
    moving = json.loads(json.dumps(FRAMES[0]))
    moving['slots'][0].update(status='unwinding', type='PETG', color=[1, 2, 3])
    def row():
        with s1.engine.connect() as c:
            return c.execute(text('SELECT filament_type,color_hex,color,loaded_at FROM filament_slots WHERE printer_id=59 AND slot_number=1')).one()
    with s1.engine.begin() as c:
        sync_ace_inventory(c, 59, parse_ace(hub(settled)))
        c.execute(text("UPDATE filament_slots SET loaded_at='2026-10-01 00:00:00' WHERE printer_id=59 AND slot_number=1"))
    before = row()
    with s1.engine.begin() as c:
        sync_ace_inventory(c, 59, parse_ace(hub(moving)))
    assert row() == before and before.filament_type == 'PLA'


@pytest.mark.parametrize('status,material', [('empty', 'PLA'), ('ready', ''), ('unwinding', ''), ('empty', '')])
def test_only_an_empty_report_or_a_blank_type_clears_a_slot(s1, status, material):
    from modules.printers.monitors.ams_inventory import sync_ace_inventory
    settled = json.loads(json.dumps(FRAMES[0]))
    cleared = json.loads(json.dumps(FRAMES[0]))
    cleared['slots'][0].update(status=status, type=material)
    with s1.engine.begin() as c:
        sync_ace_inventory(c, 59, parse_ace(hub(settled)))
        c.execute(text("UPDATE filament_slots SET do_not_feed=TRUE,do_not_feed_note='Feed fault' WHERE printer_id=59 AND slot_number=1"))
        sync_ace_inventory(c, 59, parse_ace(hub(cleared)))
    row = slot_rows(s1.engine)[1]
    assert row.filament_type == 'empty' and row.color_hex is None
    assert bool(row.do_not_feed) is True and row.do_not_feed_note == 'Feed fault'


@pytest.mark.parametrize('status,material,expected', [
    ('unwinding', 'PLA', 'PLA'), ('shifting', 'PETG', 'PETG'), ('feeding', 'Future polymer', 'Unknown'),
    ('unwinding', '', 'empty'), ('ready', 'PLA', 'PLA'), ('empty', 'PLA', 'empty'),
])
def test_a_new_slot_row_records_the_reported_type_even_in_motion(s1, status, material, expected):
    from modules.printers.monitors.ams_inventory import sync_ace_inventory
    frame = json.loads(json.dumps(FRAMES[0]))
    frame['slots'][0].update(status=status, type=material)
    with s1.engine.begin() as c:
        sync_ace_inventory(c, 59, parse_ace(hub(frame)))
    assert slot_rows(s1.engine)[1].filament_type == expected


def test_a_poll_without_an_ace_report_writes_nothing_and_is_not_usable(s1, monkeypatch):
    from modules.printers.adapters import moonraker
    s1.device.frame = _frame_at(IDLE_T)
    s1.monitor.observe()
    before = slot_rows(s1.engine)
    def no_hub(self, path, timeout=5):
        return {'result': {'status': dict(print_stats=dict(state='printing', filename='x.gcode'), webhooks=dict(state='ready'))}}
    monkeypatch.setattr(moonraker.MoonrakerPrinter, '_get', no_hub)
    status = s1.monitor.printer.get_status()
    assert status.ace_status is None and status.filament_slots == []
    s1.monitor._maybe_sync_ace = lambda *a: pytest.fail('Nothing was reported, so nothing may be persisted')
    s1.monitor._process_status(status)
    assert slot_rows(s1.engine) == before


def test_cold_prep_cannot_become_a_started_print_via_the_monitor(s1):
    s1.device.state = 'heating'
    s1.device.filename = 'cold-prep.gcode'
    s1.device.nozzle_target = 0
    s1.monitor.observe()
    assert s1.monitor._current_job_db_id is None
    with s1.engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM print_jobs')).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM jobs WHERE status='printing'")).scalar_one() == 0
    s1.device.nozzle_target = 210
    s1.monitor.observe()
    assert s1.monitor._current_job_db_id is not None
