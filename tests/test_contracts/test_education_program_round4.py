"""Protect small participant-minus-concentrator cohorts in shared exports."""
from copy import deepcopy

import pytest

from modules.organizations.education_program_rendering import safe_export
from tests.test_contracts.test_education_program_round3 import derivable, example, hidden


@pytest.mark.parametrize('difference', [1, 2, 3, 4])
def test_small_non_concentrator_cohort_cannot_be_rebuilt(difference):
    source = example([[('A', 12), ('B', 12)], [('C', 12)]])
    source['classes'][0].update(participants=6 + difference, concentrators=6)
    for row in source['classes'][1:]:
        row.update(participants=12, concentrators=6)
    source['programs'][0].update(participants=18 + difference, concentrators=12)
    source['programs'][1].update(participants=12, concentrators=6)
    source['summary'].update(participants=30 + difference, concentrators=18)
    original = deepcopy(source)

    result = safe_export(source)

    # Includes 8 participants / 6 concentrators. Parent differences exceed four,
    # so these totals must be hidden by propagation, not their own small gap.
    for row in (result['classes'][0], result['programs'][0], result['summary']):
        assert hidden(row['concentrators'])
    assert result['classes'][0]['participants'] == 6 + difference
    assert result['classes'][1]['concentrators'] == 6
    assert result['programs'][1]['concentrators'] == 6
    assert not derivable(result, 'concentrators', {'A'})
    assert source == original


@pytest.mark.parametrize('difference', [0, 1, 2, 3, 4, 5])
def test_non_concentrator_suppression_boundaries(difference):
    source = example([[('A', 12)]])
    for row in (source['summary'], *source['programs'], *source['classes']):
        row.update(participants=6 + difference, concentrators=6)

    result = safe_export(source)

    for row in (result['summary'], *result['programs'], *result['classes']):
        assert row['participants'] == 6 + difference
        if 0 < difference < 5:
            assert hidden(row['concentrators'])
        else:
            assert row['concentrators'] == 6


@pytest.mark.parametrize('participants', [1, 2, 3, 4])
def test_small_participant_count_still_links_zero_concentrators(participants):
    source = example([[('A', 12)]])
    for row in (source['summary'], *source['programs'], *source['classes']):
        row.update(participants=participants, concentrators=0)

    result = safe_export(source)

    for row in (result['summary'], *result['programs'], *result['classes']):
        assert hidden(row['participants'])
        assert hidden(row['concentrators'])
