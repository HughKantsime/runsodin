"""Original receipt age survives anchoring, precision conversion and retries."""
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from modules.printers.telemetry.bambu.ams_receipt_time import estimate_receipt_time


def estimate(**changes):
    inputs = dict(received_monotonic=10.0, query_started_monotonic=20.0,
                  query_finished_monotonic=20.5,
                  database_now=datetime(2026, 10, 12, tzinfo=timezone.utc),
                  receipt_clock_domain='trusted-test-boot',
                  current_clock_domain='trusted-test-boot')
    inputs.update(changes)
    return estimate_receipt_time(**inputs)


def test_query_latency_cannot_refresh_original_receipt():
    value = estimate()
    assert value.received_at == datetime(2026, 10, 11, 23, 59, 49, 500000,
                                         tzinfo=timezone.utc)
    assert value.provenance_version == 'db-clock-estimate-v1'
    with pytest.raises(FrozenInstanceError):
        value.received_at = datetime.now(timezone.utc)


def test_later_anchor_preserves_original_receipt_instead_of_write_time():
    first = estimate(query_finished_monotonic=20)
    later = estimate(query_started_monotonic=50, query_finished_monotonic=50,
                     database_now=datetime(2026, 10, 12, tzinfo=timezone.utc)
                     + timedelta(seconds=30))
    assert later.received_at == first.received_at


@pytest.mark.parametrize('name', ['received_monotonic', 'query_started_monotonic',
                                  'query_finished_monotonic'])
@pytest.mark.parametrize('value', [True, None, '10', float('nan'), float('inf'), -1,
                                  10 ** 1000])
def test_invalid_monotonic_samples_refuse(name, value):
    with pytest.raises(ValueError):
        estimate(**{name: value})


@pytest.mark.parametrize('changes', [
    {'query_finished_monotonic': 19},
    {'query_finished_monotonic': 21.00001},
    {'received_monotonic': 20.1},
    {'received_monotonic': 20.6},
    {'received_monotonic': 0, 'query_started_monotonic': 119.5,
     'query_finished_monotonic': 120},
    {'receipt_clock_domain': ''},
    {'receipt_clock_domain': 'old-boot'},
    {'receipt_clock_domain': None, 'current_clock_domain': None},
    {'receipt_clock_domain': 'a' * 129, 'current_clock_domain': 'a' * 129},
    {'database_now': None},
    {'database_now': datetime(2026, 10, 12)},
])
def test_inconsistent_expired_or_unavailable_anchor_refuses(changes):
    with pytest.raises(ValueError):
        estimate(**changes)


def test_precision_rounding_is_conservative():
    value = estimate(received_monotonic=20.4999999, query_started_monotonic=20.5)
    assert value.received_at == datetime(2026, 10, 12, tzinfo=timezone.utc) - timedelta(microseconds=1)


def test_non_utc_database_clock_is_normalized_and_boundary_latency_accepted():
    anchor = datetime(2026, 10, 12, tzinfo=timezone(timedelta(hours=-4)))
    value = estimate(database_now=anchor, query_finished_monotonic=21)
    assert value.received_at == anchor.astimezone(timezone.utc) - timedelta(seconds=11)
