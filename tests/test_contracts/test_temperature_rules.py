"""Pure temperature-anomaly rules: table-driven, no database, no clock."""
from datetime import datetime, timedelta

import pytest

from modules.notifications.temperature_rules import (
    BED, HEATING_WITHOUT_TARGET, NOT_REACHING_TARGET, NOZZLE, OVERSHOOT, SENSOR_FAULT, Sample, Thresholds,
    default_settings, evaluate, normalize_settings, thresholds_from, validate_update, window_minutes,
)

NOW = datetime(2026, 11, 12, 12, 0)
LONG_AGO = NOW - timedelta(hours=2)


def make(count, nozzle=220.0, bed=60.0, nozzle_target=220.0, bed_target=60.0, end=NOW):
    def value(spec, i):
        return spec(i) if callable(spec) else spec
    return [Sample(end - timedelta(minutes=count - 1 - i), value(bed, i), value(nozzle, i),
                   value(bed_target, i), value(nozzle_target, i)) for i in range(count)]


def keys(evaluation):
    return {finding.key for finding in evaluation.findings}


# (id, samples, started_at, expected finding keys)
CASES = [
    ("under: nozzle 25 C low, sustained", make(12, nozzle=195.0), LONG_AGO, {(NOZZLE, NOT_REACHING_TARGET)}),
    ("under: bed 30 C low, sustained", make(12, bed=30.0), LONG_AGO, {(BED, NOT_REACHING_TARGET)}),
    ("under: 14 C low is inside the threshold", make(12, nozzle=206.0), LONG_AGO, set()),
    ("under: only 4 samples is not sustained", make(4, nozzle=190.0), LONG_AGO, set()),
    ("under: inside the warm-up grace from print start",
     make(12, nozzle=190.0), NOW - timedelta(minutes=7), set()),
    ("under: after the grace from print start",
     make(12, nozzle=190.0), NOW - timedelta(minutes=20), {(NOZZLE, NOT_REACHING_TARGET)}),
    ("under: a target change 4 minutes ago restarts the grace",
     make(12, nozzle=220.0, nozzle_target=lambda i: 200.0 if i < 8 else 260.0), LONG_AGO, set()),
    ("under: a target change 24 minutes ago is long past",
     make(30, nozzle=190.0, nozzle_target=lambda i: 200.0 if i < 5 else 220.0), LONG_AGO, {(NOZZLE, NOT_REACHING_TARGET)}),
    ("under: a single outlier never alerts", make(12, nozzle=lambda i: 100.0 if i == 9 else 220.0), LONG_AGO, set()),
    ("under: target 0 is never under", make(12, nozzle=30.0, nozzle_target=0.0), LONG_AGO, set()),
    ("over: 20 C above, sustained", make(12, nozzle=240.0), LONG_AGO, {(NOZZLE, OVERSHOOT)}),
    ("over: 14 C above is inside the threshold", make(12, nozzle=234.0), LONG_AGO, set()),
    ("over: a lowered target while cooling is not an overshoot",
     make(12, nozzle=220.0, nozzle_target=lambda i: 220.0 if i < 8 else 180.0), LONG_AGO, set()),
    ("over: single outlier ignored", make(12, bed=lambda i: 120.0 if i == 10 else 60.0), LONG_AGO, set()),
    ("idle: nozzle heating with no target",
     make(8, nozzle=lambda i: 70.0 + 5 * i, nozzle_target=0.0), LONG_AGO, {(NOZZLE, HEATING_WITHOUT_TARGET)}),
    ("idle: bed heating with no target", make(8, bed=lambda i: 50.0 + i, bed_target=0.0), LONG_AGO, {(BED, HEATING_WITHOUT_TARGET)}),
    ("idle: cooling down never fires", make(8, nozzle=lambda i: 200.0 - 5 * i, nozzle_target=0.0), LONG_AGO, set()),
    ("idle: hot and steady never fires", make(8, nozzle=100.0, nozzle_target=0.0), LONG_AGO, set()),
    ("idle: below the idle-hot level never fires", make(8, nozzle=lambda i: 30.0 + i, nozzle_target=0.0), LONG_AGO, set()),
    ("fault: nozzle reads 400", make(12, nozzle=400.0), LONG_AGO, {(NOZZLE, SENSOR_FAULT), (NOZZLE, OVERSHOOT)}),
    ("fault: nozzle reads -50", make(12, nozzle=-50.0), LONG_AGO, {(NOZZLE, SENSOR_FAULT), (NOZZLE, NOT_REACHING_TARGET)}),
    ("fault: bed reads 160", make(12, bed=160.0), LONG_AGO, {(BED, SENSOR_FAULT), (BED, OVERSHOOT)}),
    ("fault: one absurd reading is ignored", make(12, nozzle=lambda i: 999.0 if i == 11 else 220.0), LONG_AGO, set()),
    ("fault: a reading stuck below a moved target is one fault, reported once (not reaching target)",
     make(35, nozzle=187.0, nozzle_target=lambda i: 220.0 if i < 10 else 250.0), LONG_AGO,
     {(NOZZLE, NOT_REACHING_TARGET)}),
    ("fault: a steady reading under a steady target is a held temperature", make(35), LONG_AGO, set()),
    ("fault: an unchanging ambient reading is only a cold bed",
     make(35, bed=24.0, bed_target=lambda i: 20.0 if i < 10 else 30.0), LONG_AGO, set()),
    ("no readings, nothing to judge", make(12, nozzle=None, bed=None), LONG_AGO, set()),
    ("a gap inside the window is not consecutive",
     [Sample(NOW - timedelta(minutes=m), 60.0, 190.0, 60.0, 220.0) for m in (40, 30, 20, 10, 0)], LONG_AGO, set()),
]


@pytest.mark.parametrize("name,samples,started,expected", CASES, ids=[c[0] for c in CASES])
def test_rule_table(name, samples, started, expected):
    assert keys(evaluate(samples, started, NOW)) == expected


def test_stale_data_is_not_judged():
    samples = make(12, nozzle=190.0, end=NOW - timedelta(minutes=10))
    assert evaluate(samples, LONG_AGO, NOW).findings == ()
    assert keys(evaluate(samples, LONG_AGO, NOW - timedelta(minutes=10))) == {(NOZZLE, NOT_REACHING_TARGET)}


def test_samples_before_the_print_started_are_ignored():
    samples = make(12, nozzle=190.0)
    assert evaluate(samples, NOW + timedelta(minutes=1), NOW).findings == ()


def test_under_finding_carries_actual_target_and_minutes():
    assert evaluate(make(12, nozzle=198.0), NOW - timedelta(minutes=12), NOW).findings == ()  # four minutes past the grace
    [finding] = evaluate(make(13, nozzle=198.0), NOW - timedelta(minutes=13), NOW).findings
    assert (finding.rule, finding.sensor, finding.actual, finding.target) == (NOT_REACHING_TARGET, NOZZLE, 198.0, 220.0)
    assert finding.minutes == 5 and finding.critical is False  # five elapsed minutes from the end of the 8-minute grace


@pytest.mark.parametrize("nozzle,critical", [(240.0, False), (251.0, True), (250.0, False)])
def test_overshoot_is_critical_only_above_30_c(nozzle, critical):
    [finding] = [f for f in evaluate(make(12, nozzle=nozzle), LONG_AGO, NOW).findings if f.rule == OVERSHOOT]
    assert finding.critical is critical


def test_heating_with_no_target_is_critical():
    [finding] = evaluate(make(8, nozzle=lambda i: 70.0 + 5 * i, nozzle_target=0.0), LONG_AGO, NOW).findings
    assert finding.rule == HEATING_WITHOUT_TARGET and finding.critical is True


def test_thresholds_are_honoured():
    samples = make(12, nozzle=206.0)  # 14 C low
    assert evaluate(samples, LONG_AGO, NOW).findings == ()
    assert keys(evaluate(samples, LONG_AGO, NOW, Thresholds(under_c=10.0))) == {(NOZZLE, NOT_REACHING_TARGET)}
    assert evaluate(make(8, nozzle=lambda i: 70.0 + 5 * i, nozzle_target=0.0), LONG_AGO, NOW,
                    Thresholds(idle_hot_nozzle_c=200.0)).findings == ()


# ------------------------------------------------------------ recovery (re-arm)

def test_recovery_clears_only_after_the_whole_sustain_window():
    bad_then_good = make(13, nozzle=lambda i: 190.0 if i < 8 else 220.0)  # good for four minutes
    assert (NOZZLE, NOT_REACHING_TARGET) not in evaluate(bad_then_good, LONG_AGO, NOW).clear
    good = make(14, nozzle=lambda i: 190.0 if i < 8 else 220.0)  # good for five minutes
    assert (NOZZLE, NOT_REACHING_TARGET) in evaluate(good, LONG_AGO, NOW).clear


@pytest.mark.parametrize("samples,key", [
    (make(14, nozzle=lambda i: 245.0 if i < 8 else 220.0), (NOZZLE, OVERSHOOT)),
    (make(14, nozzle=lambda i: 400.0 if i < 8 else 220.0), (NOZZLE, SENSOR_FAULT)),
    (make(14, nozzle=lambda i: 150.0 - i, nozzle_target=lambda i: 0.0), (NOZZLE, HEATING_WITHOUT_TARGET)),
])
def test_other_rules_clear_on_recovery(samples, key):
    if key[1] == HEATING_WITHOUT_TARGET:
        samples = make(14, nozzle=lambda i: 100.0 if i < 8 else 40.0, nozzle_target=0.0)
    assert key in evaluate(samples, LONG_AGO, NOW).clear


def test_a_hot_idle_nozzle_that_is_only_cooling_does_not_clear():
    assert (NOZZLE, HEATING_WITHOUT_TARGET) not in evaluate(
        make(10, nozzle=lambda i: 200.0 - 5 * i, nozzle_target=0.0), LONG_AGO, NOW).clear


# ------------------------------------------------------------ settings

def test_defaults_match_the_spec():
    assert default_settings() == {"enabled": True, "under_c": 15, "over_c": 15, "sustain_minutes": 5,
                                  "warmup_minutes": 8, "idle_hot_nozzle_c": 60, "idle_hot_bed_c": 45,
                                  "disabled_printer_ids": []}
    th = thresholds_from(default_settings())
    assert (th.under_c, th.over_c, th.sustain_minutes, th.warmup_minutes) == (15.0, 15.0, 5, 8)
    assert 13 <= window_minutes(th) <= 20   # sustain + warm-up plus a margin, never the whole history


def test_defaults_are_not_shared():
    default_settings()["disabled_printer_ids"].append(1)
    assert default_settings()["disabled_printer_ids"] == []


@pytest.mark.parametrize("body", [
    {"under_c": True}, {"under_c": 4}, {"under_c": 101}, {"under_c": "15"}, {"under_c": float("nan")},
    {"sustain_minutes": 2}, {"sustain_minutes": 5.5}, {"warmup_minutes": -1},
    {"enabled": "yes"}, {"enabled": 1}, {"nope": 1}, {"disabled_printer_ids": 3},
    {"disabled_printer_ids": [0]}, {"disabled_printer_ids": ["1"]}, {"disabled_printer_ids": [True]},
    {"disabled_printer_ids": list(range(1, 1002))}, [], None,
])
def test_validate_update_refuses(body):
    with pytest.raises(ValueError):
        validate_update(body)


def test_validate_update_merges_and_normalizes():
    merged = validate_update({"under_c": 20.0, "disabled_printer_ids": [3, 1, 3], "enabled": False}, default_settings())
    assert merged["under_c"] == 20 and merged["disabled_printer_ids"] == [1, 3] and merged["enabled"] is False
    assert merged["over_c"] == 15


def test_normalize_falls_back_per_key():
    stored = {"under_c": "bad", "over_c": 25, "enabled": False, "stray": 1, "sustain_minutes": 1}
    settings = normalize_settings(stored)
    assert settings["under_c"] == 15 and settings["over_c"] == 25 and settings["enabled"] is False
    assert settings["sustain_minutes"] == 5 and "stray" not in settings
    assert normalize_settings(None) == default_settings() and normalize_settings("x") == default_settings()


def test_fast_telemetry_must_still_violate_for_the_whole_period():
    # A monitor writing every 5 seconds: 30 low samples cover two and a half minutes, not 5.
    fast = [Sample(NOW - timedelta(seconds=5 * (29 - i)), 60.0, 190.0, 60.0, 220.0) for i in range(30)]
    assert keys(evaluate(fast, LONG_AGO, NOW)) == set()
    long_enough = [Sample(NOW - timedelta(seconds=5 * (59 - i)), 60.0, 190.0, 60.0, 220.0) for i in range(60)]
    assert keys(evaluate(long_enough, LONG_AGO, NOW)) == {(NOZZLE, NOT_REACHING_TARGET)}


def test_a_long_gap_inside_the_period_is_not_sustained():
    samples = [s for i, s in enumerate(make(12, nozzle=190.0)) if i not in (8, 9, 10)]
    assert keys(evaluate(samples, LONG_AGO, NOW)) == set()


def test_heating_with_no_target_waits_out_the_warm_up():
    rising = make(8, nozzle=lambda i: 70.0 + 5 * i, nozzle_target=0.0)
    assert keys(evaluate(rising, NOW - timedelta(minutes=7), NOW)) == set()
    assert keys(evaluate(rising, None, NOW)) == {(NOZZLE, HEATING_WITHOUT_TARGET)}


def test_a_burst_of_rows_never_shortens_the_sustained_time():
    """Codex sign-off 11-12: forty low readings five seconds apart (3 min 15 s) raised the
    five-minute alert. The configured time itself must elapse."""
    burst = [Sample(NOW - timedelta(seconds=5 * i), 60.0, 190.0, 60.0, 220.0) for i in range(40)]
    assert evaluate(burst, LONG_AGO, NOW).findings == ()
    nearly = [Sample(NOW - timedelta(seconds=10 * i), 60.0, 190.0, 60.0, 220.0) for i in range(29)]   # 4 min 40 s
    assert evaluate(nearly, LONG_AGO, NOW).findings == ()
    full = [Sample(NOW - timedelta(seconds=10 * i), 60.0, 190.0, 60.0, 220.0) for i in range(31)]     # 5 min
    assert keys(evaluate(full, LONG_AGO, NOW)) == {(NOZZLE, NOT_REACHING_TARGET)}
