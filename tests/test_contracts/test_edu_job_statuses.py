"""The Education approval states must round-trip through the ORM enum."""

from core.base import JobStatus


def test_approval_states_are_canonical_job_statuses():
    assert JobStatus.SUBMITTED.value == "submitted"
    assert JobStatus.REJECTED.value == "rejected"


def test_every_approval_route_literal_is_representable():
    values = {status.value for status in JobStatus}
    assert {"submitted", "rejected"} <= values
