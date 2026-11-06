"""Independent reconstruction checks for round-five Perkins disclosures."""
from copy import deepcopy
from fractions import Fraction
from itertools import combinations
from random import Random

import pytest

from modules.organizations.education_program_rendering import ADDITIVE_METRICS, safe_export
from modules.organizations.education_program_reports import ReportRow
from tests.test_contracts.test_education_program_round3 import example, hidden


def marked(groups):
    """Disjoint people; marks deliberately independent of print submitters."""
    report = example([[(name, students) for name, students, _, _ in group] for group in groups])
    offset = 0
    for i, (row, entry) in enumerate(zip(report['classes'], (e for g in groups for e in g))):
        name, students, participants, concentrators = entry
        row.update(participants=participants, concentrators=concentrators)
        if name.startswith('Unassigned'):
            row.update(id=None, unassigned_jobs=row['jobs'])
        report['classes'][i] = ReportRow(row, range(offset, offset + students))
        offset += students
    for row in [report['summary'], *report['programs']]:
        children = [c for c in report['classes'] if row is report['summary'] or c['cost_center_id'] == row['id']]
        for key in ('participants', 'concentrators', 'unassigned_jobs'):
            row[key] = sum(c[key] for c in children)
    report['programs'] = [ReportRow(row, set().union(*(c.students for c in report['classes']
        if c['cost_center_id'] == row['id']))) for row in report['programs']]
    report['summary'] = ReportRow(report['summary'], set().union(*(c.students for c in report['classes'])))
    return report


def review_case(seed):
    # Minimal witnesses preserving the supplied review values, not an attempt
    # to recreate the reviewer's unavailable random-generator implementation.
    if seed == 60:
        return marked([[('C10', 3, 3, 2), ('C11', 15, 15, 10), ('Unassigned1', 2, 0, 0)]])
    report = marked([[('C00', 4, 3, 0)], [('Unassigned1', 2, 0, 0)], [('C20', 15, 15, 10)]])
    report['programs'][2]['program'] = 'P2'
    return report


def oracle(report, metric):
    """Row-span oracle over leaf variables, independent of production helpers.

    Non-concentrators use the JOINT participant/concentrator system, including
    combinations with different visible rows on the two sides of the subtraction.
    Structural Unassigned zeros are public knowledge even when text is hidden.
    """
    leaves = report['classes']
    n = len(leaves)
    joint = metric == 'non_concentrators'
    width = n * (2 if joint else 1)
    equations = []
    for key in (('participants', 'concentrators') if joint else (metric,)):
        for row in [report['summary'], *report['programs'], *leaves]:
            zero = (row in leaves and row.get('id', 1) is None and key in
                    ('participants', 'concentrators', 'instructor_entered_teaching_hours'))
            if not isinstance(row[key], (int, float)) and not zero:
                continue
            if row is report['summary']:
                indices = range(n)
            elif 'cost_center_id' in row:
                indices = [i for i, leaf in enumerate(leaves) if leaf is row]
            else:
                indices = [i for i, leaf in enumerate(leaves) if leaf['cost_center_id'] == row['id']]
            vector = [Fraction(0)] * width
            for i in indices:
                vector[i + (n if joint and key == 'concentrators' else 0)] = Fraction(1)
            equations.append(vector)
    basis = {}
    for vector in equations:
        for pivot, previous in sorted(basis.items()):
            factor = vector[pivot]
            if factor:
                vector = [a - factor * b for a, b in zip(vector, previous)]
        pivot = next((i for i, v in enumerate(vector) if v), None)
        if pivot is not None:
            basis[pivot] = [v / vector[pivot] for v in vector]

    def determined(indices):
        target = [Fraction(0)] * width
        for i in indices:
            target[i] = Fraction(1)
            if joint:
                target[n + i] = Fraction(-1)
        for pivot, previous in sorted(basis.items()):
            factor = target[pivot]
            if factor:
                target = [a - factor * b for a, b in zip(target, previous)]
        return not any(target)
    return determined


@pytest.mark.parametrize('seed,indices', [(60, [0]), (26, [0, 1])])
def test_review_seed_cannot_rebuild_people(seed, indices):
    report = review_case(seed)
    result = safe_export(report)
    assert not oracle(result, 'participants')(indices)
    if seed == 60:
        assert not oracle(result, 'concentrators')(indices)
    assert not oracle(result, 'non_concentrators')(indices)


@pytest.mark.parametrize('seed', range(300))
def test_independent_marks_have_no_determined_unsafe_combination(seed):
    rng = Random(seed)
    groups = []
    for pid in range(rng.randint(1, 3)):
        group = []
        for cid in range(rng.randint(1, 2)):
            students = rng.choice((0, 1, 2, 3, 4, 6, 12, 18))
            participants = rng.randint(0, 18)
            group.append((f'C{pid}{cid}', students, participants, rng.randint(0, participants)))
        group.append((f'Unassigned{pid}', rng.randint(0, 6), 0, 0))
        groups.append(group)
    report = marked(groups)
    original = deepcopy(report)
    result = safe_export(report)
    assert report == original
    assert result == safe_export(report)
    for metric in (*ADDITIVE_METRICS, 'non_concentrators'):
        determined = oracle(result, metric)
        for size in range(1, len(report['classes']) + 1):
            for indices in combinations(range(len(report['classes'])), size):
                rows = [report['classes'][i] for i in indices]
                if metric in ('participants', 'concentrators', 'non_concentrators'):
                    value = sum(r['participants'] - r['concentrators'] if metric == 'non_concentrators'
                                else r[metric] for r in rows)
                    unsafe = 0 < value < 5
                else:
                    # Only hidden cells are protected; public zeros are allowed.
                    if not all(hidden(result['classes'][i][metric]) for i in indices):
                        continue
                    unsafe = size == 1 or len(set().union(*(r.students for r in rows))) < 5
                    if metric == 'instructor_entered_teaching_hours' and all(r['id'] is None for r in rows):
                        continue  # structural zero
                if unsafe:
                    assert not determined(indices), (seed, metric, indices, groups)


def test_virtual_metric_protects_small_rest_when_both_people_sums_are_large():
    report = marked([[('A', 4, 8, 5), ('Unassigned1', 2, 0, 0)], [('B', 15, 15, 10)]])
    result = safe_export(report)
    # The hidden rest is 8 participants and 5 concentrators: individually safe,
    # but their difference is just three non-concentrators.
    assert not oracle(result, 'non_concentrators')([0, 1])


def test_unassigned_known_zero_cannot_pad_teaching_hour_protection():
    report = marked([[('A', 3, 0, 0), ('B', 15, 0, 0), ('Unassigned1', 2, 0, 0)]])
    for row, hours in zip(report['classes'], (30, 150, 0)):
        row['instructor_entered_teaching_hours'] = hours
    for row in [report['summary'], *report['programs']]:
        row['instructor_entered_teaching_hours'] = 180
    assert not oracle(safe_export(report), 'instructor_entered_teaching_hours')([0])
