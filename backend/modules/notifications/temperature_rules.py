"""Pure temperature-anomaly rules over printer_telemetry samples.

No I/O and no clock: callers pass the samples, the print's start, "now" and the thresholds. Every
timestamp is naive UTC, as stored. Every rule needs `sustain_minutes` consecutive samples, so one bad
reading never alerts. Findings are keyed (sensor, rule); `Evaluation.clear` lists the keys whose
condition has been absent for the whole sustain window, which is what re-arms an alert.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, NamedTuple, Optional, Sequence

NOZZLE, BED = "nozzle", "bed"
SENSORS = (NOZZLE, BED)

NOT_REACHING_TARGET = "not_reaching_target"
OVERSHOOT = "overshoot"
HEATING_WITHOUT_TARGET = "heating_without_target"
SENSOR_FAULT = "sensor_fault"
RULES = (NOT_REACHING_TARGET, OVERSHOOT, HEATING_WITHOUT_TARGET, SENSOR_FAULT)

SANE_RANGE = {NOZZLE: (-10.0, 350.0), BED: (-10.0, 150.0)}
FROZEN_MINUTES = 30          # an identical reading this long, while the target moved, is a frozen sensor
FROZEN_TARGET_SPAN_C = 5.0   # "the target moved": its extremes in the window differ by more than this
AMBIENT_C = 35.0             # an unchanging reading at or below this is just a cold sensor
STALE_MINUTES = 6            # newest sample older than this: the printer is not reporting, judge nothing
GAP_SLACK_MINUTES = 2        # N consecutive samples may span N + this many minutes, no more
TARGET_STEP_C = 0.5          # a target moving by more than this is a target change
RISE_C = 2.0                 # net rise over the window that counts as "rising"
RISE_DIP_C = 1.0             # a step down larger than this inside the window means not rising
CRITICAL_OVER_C = 30.0       # overshoot above this is critical

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "under_c": 15,
    "over_c": 15,
    "sustain_minutes": 5,
    "warmup_minutes": 8,
    "idle_hot_nozzle_c": 60,
    "idle_hot_bed_c": 45,
    "disabled_printer_ids": [],
}
BOUNDS = {
    "under_c": (5, 100),
    "over_c": (5, 100),
    "sustain_minutes": (3, 60),
    "warmup_minutes": (0, 60),
    "idle_hot_nozzle_c": (40, 200),
    "idle_hot_bed_c": (30, 120),
}
_WHOLE = {"sustain_minutes", "warmup_minutes"}
MAX_DISABLED_PRINTERS = 1000


class Sample(NamedTuple):
    at: datetime
    bed: Optional[float]
    nozzle: Optional[float]
    bed_target: Optional[float]
    nozzle_target: Optional[float]


@dataclass(frozen=True)
class Thresholds:
    under_c: float = 15.0
    over_c: float = 15.0
    sustain_minutes: int = 5
    warmup_minutes: int = 8
    idle_hot_nozzle_c: float = 60.0
    idle_hot_bed_c: float = 45.0

    def idle_hot_c(self, sensor: str) -> float:
        return self.idle_hot_nozzle_c if sensor == NOZZLE else self.idle_hot_bed_c


@dataclass(frozen=True)
class Finding:
    rule: str
    sensor: str
    actual: float
    target: Optional[float]
    minutes: int
    critical: bool = False
    detail: str = ""  # sensor_fault only: "range" or "frozen"

    @property
    def key(self) -> tuple[str, str]:
        return (self.sensor, self.rule)


@dataclass(frozen=True)
class Evaluation:
    findings: tuple[Finding, ...] = ()
    clear: frozenset = frozenset()


class _Point(NamedTuple):
    at: datetime
    actual: float
    target: Optional[float]


# ---------------------------------------------------------------- settings (pure)

def default_settings() -> dict[str, Any]:
    settings = dict(DEFAULT_SETTINGS)
    settings["disabled_printer_ids"] = []
    return settings


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _check(key: Any, value: Any) -> Any:
    if key == "enabled":
        if not isinstance(value, bool):
            raise ValueError("enabled must be true or false")
        return value
    if key == "disabled_printer_ids":
        if (not isinstance(value, list) or len(value) > MAX_DISABLED_PRINTERS
                or not all(isinstance(i, int) and not isinstance(i, bool) and i > 0 for i in value)):
            raise ValueError("disabled_printer_ids must be a list of printer ids")
        return sorted(set(value))
    if key in BOUNDS:
        low, high = BOUNDS[key]
        if not _is_number(value) or not low <= value <= high:
            raise ValueError(f"{key} must be a number from {low} to {high}")
        if key in _WHOLE and value != int(value):
            raise ValueError(f"{key} must be a whole number")
        return int(value) if float(value).is_integer() else float(value)
    raise ValueError(f"Unknown setting: {key}")


def normalize_settings(raw: Any) -> dict[str, Any]:
    """Stored settings with defaults filled in; an invalid stored value falls back to its default."""
    settings = default_settings()
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            try:
                settings[key] = _check(key, value)
            except ValueError:
                continue
    return settings


def validate_update(body: Any, current: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Strictly validate a partial update over the current settings; raises ValueError."""
    if not isinstance(body, Mapping):
        raise ValueError("Settings must be an object")
    merged = dict(current) if current is not None else default_settings()
    for key, value in body.items():
        merged[key] = _check(key, value)
    return merged


def thresholds_from(settings: Mapping[str, Any]) -> Thresholds:
    return Thresholds(
        under_c=float(settings["under_c"]), over_c=float(settings["over_c"]),
        sustain_minutes=int(settings["sustain_minutes"]), warmup_minutes=int(settings["warmup_minutes"]),
        idle_hot_nozzle_c=float(settings["idle_hot_nozzle_c"]), idle_hot_bed_c=float(settings["idle_hot_bed_c"]),
    )


def window_minutes(thresholds: Thresholds) -> int:
    """How far back the checker must read: the longest look-back any rule uses, plus a margin."""
    return max(thresholds.sustain_minutes + thresholds.warmup_minutes, FROZEN_MINUTES) + 2


# ---------------------------------------------------------------- helpers

def _num(value: Any) -> Optional[float]:
    return float(value) if _is_number(value) else None


def _points(samples: Sequence[Sample], sensor: str, started_at: Optional[datetime]) -> list[_Point]:
    points = []
    for sample in sorted(samples, key=lambda s: s.at):
        if started_at is not None and sample.at < started_at:
            continue
        actual = _num(sample.nozzle if sensor == NOZZLE else sample.bed)
        if actual is None:
            continue
        points.append(_Point(sample.at, actual, _num(sample.nozzle_target if sensor == NOZZLE else sample.bed_target)))
    return points


def _tail(points: list[_Point], count: int) -> Optional[list[_Point]]:
    """The last `count` points, if they really are consecutive (no long gap inside them)."""
    if len(points) < count:
        return None
    tail = points[-count:]
    if tail[-1].at - tail[0].at > timedelta(minutes=count + GAP_SLACK_MINUTES):
        return None
    return tail


def _last_target_change(points: list[_Point]) -> Optional[datetime]:
    change = None
    for before, after in zip(points, points[1:]):
        if before.target is not None and after.target is not None and abs(after.target - before.target) > TARGET_STEP_C:
            change = after.at
    return change


def _sustained(tail: list[_Point], violates: Callable[[_Point], Optional[bool]]) -> tuple[bool, bool]:
    """(every point violates, no point violates); None (unknown) counts as neither."""
    flags = [violates(point) for point in tail]
    return all(flag is True for flag in flags), all(flag is False for flag in flags)


def _run_minutes(points: list[_Point], violates: Callable[[_Point], Optional[bool]], floor: int,
                 since: Optional[datetime] = None) -> int:
    run = []
    for point in reversed(points):
        if violates(point) is not True or (since is not None and point.at < since):
            break
        run.append(point)
    if not run:
        return floor
    return max(floor, int(round((run[0].at - run[-1].at).total_seconds() / 60)) + 1)


def _grace_end(starts: Sequence[Optional[datetime]], warmup_minutes: int) -> Optional[datetime]:
    known = [moment for moment in starts if moment is not None]
    return max(known) + timedelta(minutes=warmup_minutes) if known else None


# ---------------------------------------------------------------- rules: each returns (finding, clear)

def not_reaching_target(sensor: str, points: list[_Point], started_at: Optional[datetime],
                        th: Thresholds) -> tuple[Optional[Finding], bool]:
    tail = _tail(points, th.sustain_minutes)
    if tail is None:
        return None, False

    def low(point: _Point) -> Optional[bool]:
        return None if point.target is None else point.target > 0 and point.target - point.actual > th.under_c

    firing, clear = _sustained(tail, low)
    if not firing:
        return None, clear
    grace_end = _grace_end((started_at, _last_target_change(points)), th.warmup_minutes)
    if grace_end is not None and tail[0].at < grace_end:
        return None, False
    last = tail[-1]
    return Finding(NOT_REACHING_TARGET, sensor, last.actual, last.target,
                   _run_minutes(points, low, th.sustain_minutes, grace_end)), False


def overshoot(sensor: str, points: list[_Point], th: Thresholds) -> tuple[Optional[Finding], bool]:
    tail = _tail(points, th.sustain_minutes)
    if tail is None:
        return None, False

    def high(point: _Point) -> Optional[bool]:
        return None if point.target is None else point.target > 0 and point.actual - point.target > th.over_c

    firing, clear = _sustained(tail, high)
    if not firing:
        return None, clear
    # A lowered target leaves the heater cooling toward it: the grace restarts at a target change only.
    grace_end = _grace_end((_last_target_change(points),), th.warmup_minutes)
    if grace_end is not None and tail[0].at < grace_end:
        return None, False
    last = tail[-1]
    return Finding(OVERSHOOT, sensor, last.actual, last.target,
                   _run_minutes(points, high, th.sustain_minutes, grace_end),
                   critical=last.actual - last.target > CRITICAL_OVER_C), False


def heating_without_target(sensor: str, points: list[_Point], th: Thresholds) -> tuple[Optional[Finding], bool]:
    tail = _tail(points, th.sustain_minutes)
    if tail is None:
        return None, False
    hot = th.idle_hot_c(sensor)

    def idle_hot(point: _Point) -> Optional[bool]:
        return None if point.target is None else point.target <= 0 and point.actual > hot

    firing, clear = _sustained(tail, idle_hot)
    if not firing:
        return None, clear
    values = [point.actual for point in tail]
    rising = values[-1] - values[0] >= RISE_C and all(after - before >= -RISE_DIP_C for before, after in zip(values, values[1:]))
    if not rising:
        return None, False  # hot and cooling, or hot and steady: not an alert, and not a recovery either
    last = tail[-1]
    return Finding(HEATING_WITHOUT_TARGET, sensor, last.actual, last.target,
                   _run_minutes(points, idle_hot, th.sustain_minutes), critical=True), False


def _frozen_window(points: list[_Point]) -> Optional[list[_Point]]:
    last = points[-1]
    window = [point for point in points if point.at >= last.at - timedelta(minutes=FROZEN_MINUTES)]
    if len(window) < FROZEN_MINUTES * 0.8:
        return None
    if last.at - window[0].at < timedelta(minutes=FROZEN_MINUTES - GAP_SLACK_MINUTES):
        return None
    if last.actual <= AMBIENT_C or any(point.actual != last.actual for point in window):
        return None
    targets = [point.target for point in window if point.target is not None]
    if not targets or max(targets) - min(targets) <= FROZEN_TARGET_SPAN_C:
        return None
    return window


def sensor_fault(sensor: str, points: list[_Point], th: Thresholds) -> tuple[Optional[Finding], bool]:
    tail = _tail(points, th.sustain_minutes)
    if tail is None:
        return None, False
    low, high = SANE_RANGE[sensor]

    def out_of_range(point: _Point) -> bool:
        return point.actual < low or point.actual > high

    firing, in_range = _sustained(tail, out_of_range)
    last = tail[-1]
    if firing:
        return Finding(SENSOR_FAULT, sensor, last.actual, last.target,
                       _run_minutes(points, out_of_range, th.sustain_minutes), detail="range"), False
    # A reading that never moves while the target does is not judged here: a heater stuck below
    # its target looks exactly the same and is already reported by "not reaching target", so a
    # separate frozen-sensor rule would alert twice for one fault.
    return None, in_range


# ---------------------------------------------------------------- entry point

def evaluate(samples: Sequence[Sample], started_at: Optional[datetime], now: datetime,
             thresholds: Optional[Thresholds] = None) -> Evaluation:
    """Judge one printer's samples from its running print. Pure: same inputs, same findings."""
    th = thresholds or Thresholds()
    findings: list[Finding] = []
    clear: set[tuple[str, str]] = set()
    for sensor in SENSORS:
        points = _points(samples, sensor, started_at)
        if not points or now - points[-1].at > timedelta(minutes=STALE_MINUTES):
            continue
        results = (
            (NOT_REACHING_TARGET, not_reaching_target(sensor, points, started_at, th)),
            (OVERSHOOT, overshoot(sensor, points, th)),
            (HEATING_WITHOUT_TARGET, heating_without_target(sensor, points, th)),
            (SENSOR_FAULT, sensor_fault(sensor, points, th)),
        )
        for rule, (finding, recovered) in results:
            if finding is not None:
                findings.append(finding)
            elif recovered:
                clear.add((sensor, rule))
    return Evaluation(tuple(findings), frozenset(clear))
