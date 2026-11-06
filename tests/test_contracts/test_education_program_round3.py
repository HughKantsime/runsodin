"""Round-three export regressions attempt reconstruction from every visible row."""
from copy import deepcopy
import csv
from fractions import Fraction
from html import escape
from io import BytesIO, StringIO
from itertools import combinations
import json
from random import Random

import pytest
from sqlalchemy import text

from modules.organizations.education_program_rendering import (
    METRICS, csv_report, email_report, label, pdf_report, safe_export,
)
from modules.organizations.education_program_reports import ReportRow, build_report
from tests.test_contracts.test_education_program_reports import program, filters
from tests.test_contracts.test_education_classes import setup
from tests.test_contracts.test_education_review_workflow import review_db, _principal


def hidden(value):
    return isinstance(value, str) and value.startswith('Suppressed')


def example(groups):
    """Disjoint students, all completed jobs; each tuple is (title, students)."""
    def metrics(students):
        row = dict.fromkeys(METRICS, 0)
        row.update(students_served=students, jobs=students * 2,
                   completed_jobs=students * 2, print_hours=students * 8,
                   material_grams=students * 30, cost=students * 10,
                   success_rate=100 if students else None)
        return row
    programs, classes = [], []
    for pid, group in enumerate(groups, 1):
        programs.append(dict(metrics(sum(n for _, n in group)), id=pid, program=f'Program {pid}'))
        for title, students in group:
            classes.append(dict(metrics(students), id=title, cost_center_id=pid,
                                program=f'Program {pid}', title=title, term='Fall'))
    return dict(notice='Example.', template='generic', generated_at='2026-11-06',
                currency='USD', basis={}, filters=dict(start_date='2026-10-01', end_date='2026-10-31'),
                summary=metrics(sum(p['students_served'] for p in programs)),
                programs=programs, classes=classes)


def derivable(report, metric, targets):
    """Can ANY linear combination of published aggregates recover the target?

    Each class is an independent variable. Gaussian elimination checks the row
    span, covering nested/mixed subtractions as well as the obvious total - A.
    No suppression implementation helpers are used by this oracle.
    """
    leaves = report['classes']
    n = len(leaves)
    equations = []
    for row in [report['summary'], *report['programs'], *leaves]:
        if not isinstance(row[metric], (int, float)):
            continue
        if row is report['summary']:
            coefficients = [1] * n
        elif 'cost_center_id' in row:
            coefficients = [int(leaf is row) for leaf in leaves]
        else:
            coefficients = [int(leaf['cost_center_id'] == row['id']) for leaf in leaves]
        equations.append(coefficients)
    basis = {}
    for vector in equations:
        vector = list(map(Fraction, vector))
        for pivot, previous in sorted(basis.items()):
            factor = vector[pivot]
            vector = [a - factor * b for a, b in zip(vector, previous)]
        pivot = next((i for i, v in enumerate(vector) if v), None)
        if pivot is not None:
            basis[pivot] = [v / vector[pivot] for v in vector]
    target = [Fraction(leaf['title'] in targets) for leaf in leaves]
    for pivot, previous in sorted(basis.items()):
        factor = target[pivot]
        target = [a - factor * b for a, b in zip(target, previous)]
    return not any(target)


@pytest.mark.parametrize('metric', ['jobs', 'completed_jobs', 'print_hours', 'material_grams', 'cost'])
@pytest.mark.parametrize('groups,targets', [
    ([[('A', 12), ('B', 1), ('C', 2)]], {'B', 'C'}),
    ([[('A', 12), ('B', 1)], [('C', 10), ('D', 2)]], {'B', 'D'}),
    ([[('A', 12), ('Unassigned', 1)]], {'Unassigned'}),
])
def test_subtraction_cannot_rebuild_small_hidden_sum(metric, groups, targets):
    source = example(groups)
    original = deepcopy(source)
    result = safe_export(source)
    assert not derivable(result, metric, targets)
    for target in targets:
        assert not derivable(result, metric, {target})
    assert source == original


@pytest.mark.parametrize('failures', [0, 6])
def test_normal_classes_keep_jobs_and_success_rate_with_zero_unknowns(failures):
    source = example([[('A', 12), ('B', 10), ('Unassigned', 0)]])
    source['classes'][-1]['id'] = None
    for row in (source['summary'], source['programs'][0], source['classes'][0]):
        row['failed_jobs'] = failures
        row['completed_jobs'] -= failures
        row['success_rate'] = round(100 * row['completed_jobs'] / row['jobs'], 2)
    result = safe_export(source)
    for before, after in zip([source['summary'], *source['programs'], *source['classes']],
                             [result['summary'], *result['programs'], *result['classes']]):
        for key in ('jobs', 'completed_jobs', 'failed_jobs', 'success_rate', 'unassigned_jobs',
                    'unknown_hours_jobs', 'unknown_material_jobs', 'unknown_cost_jobs',
                    'unknown_approved_hours_jobs'):
            assert after[key] == before[key]


def test_program_unassigned_job_alias_cannot_reveal_a_small_cohort():
    source = example([[('A', 12), ('Unassigned', 1)]])
    unassigned = source['classes'][-1]
    unassigned.update(id=None, jobs=8, completed_jobs=8, unassigned_jobs=8)
    for row in (source['summary'], source['programs'][0]):
        row.update(jobs=32, completed_jobs=32, unassigned_jobs=8)
    result = safe_export(source)
    for row in (result['summary'], result['programs'][0], result['classes'][-1]):
        assert hidden(row['unassigned_jobs'])
    assert not derivable(result, 'jobs', {'Unassigned'})


@pytest.mark.parametrize('overlap', [False, True])
def test_suppressed_cohort_union_counts_students_once(overlap):
    source = example([[('A', 12), ('B', 3), ('C', 3)]])
    cohorts = [range(12), range(12, 15), range(12, 15) if overlap else range(15, 18)]
    source['classes'] = [ReportRow(row, students) for row, students in zip(source['classes'], cohorts)]
    result = safe_export(source)
    # Six distinct students may be recoverable together; three repeated students
    # must not be. This catches summing counts after they were suppressed, too.
    assert derivable(result, 'print_hours', {'B', 'C'}) is (not overlap)
    assert all(not derivable(result, 'print_hours', {name}) for name in ('B', 'C'))


def test_mixed_visible_program_and_class_subtraction_cannot_rebuild_small_sum():
    source = example([[('A', 12), ('B', 1)], [('C', 10), ('D', 1)],
                      [('E', 12), ('F', 6), ('G', 6)]])
    # Program 3 can safely reveal F+G (12 students), but subtracting that whole
    # visible program from the summary must not isolate B+D (two students).
    for row in source['classes'][-2:]:
        row['print_hours'] = 2
    source['programs'][2]['print_hours'] = 100
    source['summary']['print_hours'] = 292
    result = safe_export(source)
    assert not derivable(result, 'print_hours', {'B', 'D'})


def test_varied_cohorts_resist_all_small_subset_subtractions():
    rng = Random(3103)
    for _ in range(60):
        groups = [[(f'{pid}-{i}', rng.choice((1, 2, 3, 6, 12))) for i in range(2)] for pid in range(3)]
        source = example(groups)
        offset = 0
        for i, row in enumerate(source['classes']):
            count = row['students_served']
            row['print_hours'] = rng.choice((0, 2, count * 8))
            source['classes'][i] = ReportRow(row, range(offset, offset + count))
            offset += count
        for row in source['programs']:
            row['print_hours'] = sum(c['print_hours'] for c in source['classes'] if c['cost_center_id'] == row['id'])
        source['summary']['print_hours'] = sum(p['print_hours'] for p in source['programs'])
        result = safe_export(source)
        hidden_rows = [row for row in result['classes'] if hidden(row['print_hours'])]
        for size in range(1, len(hidden_rows) + 1):
            for subset in combinations(hidden_rows, size):
                if size == 1 or len(set().union(*(row.students for row in subset))) < 5:
                    assert not derivable(result, 'print_hours', {r['title'] for r in subset}), (groups, subset)


@pytest.mark.parametrize('completed,failed', [(2, 0), (0, 2), (2, 10), (10, 2)])
def test_hidden_terminal_count_cannot_be_rebuilt_from_jobs_or_rate(completed, failed):
    source = example([[('A', 12), ('B', 10)]])
    row = source['classes'][0]
    row.update(jobs=completed + failed, completed_jobs=completed, failed_jobs=failed,
               success_rate=100 * completed / (completed + failed))
    result = safe_export(source)['classes'][0]
    assert hidden(result['success_rate'])
    assert sum(hidden(result[k]) for k in ('jobs', 'completed_jobs', 'failed_jobs')) >= 2
    # Visible terminal zero would give away a suppressed rate as 0% or 100%.
    assert result['completed_jobs'] != 0 and result['failed_jobs'] != 0


@pytest.mark.parametrize('key', ['unknown_hours_jobs', 'unknown_material_jobs', 'unknown_cost_jobs',
                                 'unknown_approved_hours_jobs', 'unassigned_jobs'])
def test_small_unknown_counts_do_not_blank_status_metrics(key):
    report = example([[('A', 12), ('B', 10)]])
    report['classes'][0][key] = 2
    result = safe_export(report)
    assert hidden(result['classes'][0][key])
    assert result['classes'][0]['jobs'] == 24
    assert result['classes'][0]['success_rate'] == 100


def test_unassigned_real_bucket_has_every_metric_and_respects_filters(program):
    db, cls, _ = program
    db.execute(text('DELETE FROM education_assignment_submissions WHERE submission_id=14'))
    db.commit()
    result = build_report(db, _principal(2), filters(cost_center_id=7))
    unassigned = next(row for row in result['classes'] if row['title'] == 'Unassigned')
    assert unassigned['id'] is None and unassigned['cost_center_id'] == 7
    assert unassigned['students_served'] == 1
    assert all(unassigned[k] == result['programs'][0][k] for k in METRICS)
    assert all(hidden(safe_export(result)['classes'][-1][k]) for k in METRICS)
    for scope in ({'term': cls['term']}, {'class_id': cls['id']}):
        narrowed = build_report(db, _principal(2), filters(**scope))
        assert not any(row['title'] == 'Unassigned' for row in narrowed['classes'])
    assert 'submitted_by' not in json.dumps(result)
    from fastapi.encoders import jsonable_encoder
    serialized = jsonable_encoder(result)
    assert serialized == json.loads(json.dumps(result))
    assert not any('students' in row for row in serialized['classes'])


def test_all_formats_publish_the_same_suppressed_projection():
    from pypdf import PdfReader
    source = example([[('A', 12), ('B', 1), ('Unassigned', 2)], [('C', 10), ('D', 1)]])
    source['classes'][2]['id'] = None
    expected = safe_export(source)
    rows = [expected['summary'], *expected['programs'], *expected['classes']]
    csv_rows = list(csv.reader(StringIO(csv_report(source).decode('utf-8-sig'))))
    heading = next(i for i, row in enumerate(csv_rows) if row[:2] == ['scope', 'program'])
    assert [r[4:] for r in csv_rows[heading + 1:]] == [
        [str(row[k]) if row[k] is not None else 'Unknown' for k in METRICS] for row in rows]
    pdf = '\n'.join(p.extract_text() for p in PdfReader(BytesIO(pdf_report(source))).pages)
    email = email_report(source)
    assert 'Unassigned' in pdf and 'Unassigned' in email
    for row in rows:
        for key in METRICS:
            value = str(row[key]) if row[key] is not None else 'Unknown'
            assert f'{label(expected, key)}: {value}' in pdf
            if row is expected['summary']:
                assert f'<td>{escape(label(expected, key))}</td><td>{escape(value)}</td>' in email
            else:
                assert f'{escape(label(expected, key))}={escape(value)}' in email
    assert not derivable(expected, 'print_hours', {'B', 'Unassigned', 'D'})
