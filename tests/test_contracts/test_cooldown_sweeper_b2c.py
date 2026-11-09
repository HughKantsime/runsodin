"""B2c: the API-process backstop for a smart-plug cooldown timer a monitor restart lost."""
from datetime import timedelta

import pytest
from sqlalchemy import event, text

from modules.printers import cooldown_sweeper, smart_plug
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_license_policy_b1 import policy_db, pick  # noqa: F401
from tests.test_contracts.test_license_lock_b2b_exempt_attempt import _attempt, _telemetry, _utcnow
from tests.test_contracts.test_power_fence_runtime import runtime  # noqa: F401
from tests.test_contracts.test_api_token_delegation_behavior import token_client  # noqa: F401

NINE, TEN = 9, 10


@pytest.fixture
def plug(policy_db, monkeypatch):
    monkeypatch.setattr(smart_plug, 'engine', policy_db.get_bind())
    monkeypatch.setattr(cooldown_sweeper, '_attempted', set())
    monkeypatch.setattr(smart_plug, '_cooldown_timers', {})
    sent = []
    monkeypatch.setattr(smart_plug, 'power_off', lambda pid, expected_config=None, **k: sent.append((pid, expected_config)) or True)

    def configure(plug_type='homeassistant', power_state=None, cooldown=5):
        policy_db.execute(text(
            "UPDATE printers SET plug_type=:t, plug_host='http://ha.local', plug_entity_id='switch.p9', "
            "plug_auto_off=1, plug_cooldown_minutes=:c, plug_power_state=:s WHERE id=9"),
            {'t': plug_type, 'c': cooldown, 's': power_state})
        policy_db.commit()
    configure()
    return sent, configure


def _completed(db, printer_id=NINE, minutes_ago=8):
    row = _attempt(db, printer_id, started_ago=3600)
    db.execute(text("UPDATE print_jobs SET status='completed', ended_at=:t WHERE id=:id"),
               {'t': _utcnow() - timedelta(minutes=minutes_ago), 'id': row['id']})
    db.commit()
    _telemetry(db, printer_id, state='IDLE')
    return row['id']


def test_turns_the_plug_off_once_when_the_timer_was_lost(policy_db, plug):
    sent, _ = plug
    _completed(policy_db)
    assert cooldown_sweeper.sweep() == 1
    assert sent == [(NINE, ('homeassistant', 'http://ha.local', 'switch.p9', None))]
    assert cooldown_sweeper.sweep() == 0 and len(sent) == 1  # one attempt per print


def test_waits_for_cooldown_plus_two_minutes(policy_db, plug):
    sent, _ = plug
    job = _completed(policy_db, minutes_ago=6)  # cooldown 5 + 2 not reached
    assert cooldown_sweeper.sweep() == 0 and sent == []
    policy_db.execute(text('UPDATE print_jobs SET ended_at=:t WHERE id=:id'),
                      {'t': _utcnow() - timedelta(minutes=7, seconds=5), 'id': job})
    policy_db.commit()
    _telemetry(policy_db, NINE, state='IDLE')
    assert cooldown_sweeper.sweep() == 1 and len(sent) == 1


def test_leaves_an_old_print_alone(policy_db, plug):
    sent, _ = plug
    _completed(policy_db, minutes_ago=5 + 2 + cooldown_sweeper.WINDOW_MINUTES + 1)
    assert cooldown_sweeper.sweep() == 0 and sent == []


def test_skips_a_plug_already_off(policy_db, plug):
    sent, configure = plug
    configure(power_state=0)  # the timer (or a person) already turned it off
    _completed(policy_db)
    assert cooldown_sweeper.sweep() == 0 and sent == []


def test_skips_while_a_print_is_running(policy_db, plug):
    sent, _ = plug
    _completed(policy_db)
    _attempt(policy_db, NINE, started_ago=30)  # a later print is running
    _telemetry(policy_db, NINE, state='RUNNING')
    assert cooldown_sweeper.sweep() == 0 and sent == []


def test_skips_when_telemetry_is_not_safe(policy_db, plug):
    sent, _ = plug
    _completed(policy_db)
    _telemetry(policy_db, NINE, state='IDLE', seen_ago=600)  # stale: _auto_off_safe refuses
    cooldown_sweeper.sweep()
    assert sent == []


def test_skips_when_the_license_refuses_the_completion_action(policy_db, plug, monkeypatch):
    from core import license_lock
    sent, _ = plug
    pick(policy_db, users=[3], printers=[TEN])  # 9 locked before this print: no exemption
    job = _completed(policy_db)
    calls = []
    real = license_lock.completion_allowed
    monkeypatch.setattr(license_lock, 'completion_allowed',
                        lambda engine, pid, jid, action: calls.append((pid, jid, action)) or real(engine, pid, jid, action))
    cooldown_sweeper.sweep()
    assert calls == [(NINE, job, 'cooldown_off')] and sent == []


def test_does_not_race_this_process_timer(policy_db, plug):
    sent, _ = plug
    _completed(policy_db)
    smart_plug._cooldown_timers[NINE] = object()  # the in-process timer still owns this print
    assert cooldown_sweeper.sweep() == 0 and sent == []
    del smart_plug._cooldown_timers[NINE]
    assert cooldown_sweeper.sweep() == 1 and len(sent) == 1  # not marked while the timer owned it


def test_after_the_timer_turned_it_off_nothing_is_sent(policy_db, plug):
    sent, _ = plug
    _completed(policy_db)
    smart_plug._update_power_state(NINE, False)  # what power_off records after the timer's command
    assert cooldown_sweeper.sweep() == 0 and sent == []


def test_tasmota_uses_the_power_fence_with_the_print_as_physical_id(policy_db, plug, monkeypatch):
    from modules.printers import power_fence
    sent, configure = plug
    configure(plug_type='tasmota')
    job = _completed(policy_db)
    snapshots, runs = [], []
    monkeypatch.setattr(power_fence, '_snapshot', lambda module, pid, purpose, physical, admission=True:
                        snapshots.append((pid, purpose, physical, admission)) or ('facts', 'config'))
    monkeypatch.setattr(power_fence, 'run', lambda module, pid, purpose, **k: runs.append((pid, purpose, k)) or False)
    assert cooldown_sweeper.sweep() == 1
    assert snapshots == [(NINE, 'cooldown_off', job, False)]
    assert runs == [(NINE, 'cooldown_off', {'physical_id': job, 'expected': ('facts', 'config')})]
    assert sent == []  # never the unfenced path for Tasmota


def test_tasmota_end_to_end_lost_timer_then_no_second_command(runtime, policy_db, tier_license, monkeypatch):
    """Real 027 fence: the sweeper sends the OFF a lost timer would have sent, and a
    second sweeper (another API process) or the timer finds the recorded command and
    sends nothing."""
    from core import license_lock
    module, engine, calls, workers = runtime
    monkeypatch.setattr(cooldown_sweeper, '_attempted', set())
    tier_license('enterprise', max_users=1, max_printers=3)  # 599 is a member
    license_lock.reconcile(policy_db)
    policy_db.commit()
    module.on_print_start(599, print_job_id=599)
    assert calls == ['ON']
    with engine.begin() as conn:
        conn.execute(text("UPDATE printers SET gcode_state='IDLE',last_seen=CURRENT_TIMESTAMP WHERE id=599"))
        conn.execute(text("UPDATE print_jobs SET status='completed',ended_at=:t WHERE id=599"),
                     {'t': _utcnow() - timedelta(minutes=3)})  # cooldown 0 + 2 min passed
    assert cooldown_sweeper.sweep() == 1
    assert calls == ['ON', 'OFF']
    monkeypatch.setattr(cooldown_sweeper, '_attempted', set())
    with engine.begin() as conn:
        conn.execute(text("UPDATE printers SET plug_power_state=NULL WHERE id=599"))  # state unknown again
    cooldown_sweeper.sweep()
    assert calls == ['ON', 'OFF']


def test_rollout_off_runs_no_queries(policy_db, plug, monkeypatch):
    sent, _ = plug
    _completed(policy_db)
    monkeypatch.delenv('ODIN_LICENSE_FLEET_LOCK_ENABLED')
    seen = []
    listener = lambda *a, **k: seen.append(a[2])
    engine = policy_db.get_bind()
    event.listen(engine, 'before_cursor_execute', listener)
    try:
        assert cooldown_sweeper.sweep() == 0
    finally:
        event.remove(engine, 'before_cursor_execute', listener)
    assert seen == [] and sent == []
