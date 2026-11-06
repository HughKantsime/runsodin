"""One aggregate projection for CSV, PDF and scheduled email; no student names."""
import csv
import io
from copy import deepcopy
from html import escape

from modules.organizations.education_program_disclosure import DisclosureSystem

METRICS = ('students_served', 'jobs', 'unassigned_jobs', 'completed_jobs', 'failed_jobs', 'print_hours', 'material_grams',
    'cost', 'success_rate', 'instructor_entered_teaching_hours', 'print_hours_approved_by_instructors', 'unknown_approved_hours_jobs', 'participants', 'concentrators',
    'unknown_hours_jobs', 'unknown_material_jobs', 'unknown_cost_jobs', 'excluded_unproved_jobs')

LINKED_COUNTS = ('jobs', 'completed_jobs', 'failed_jobs')
ADDITIVE_METRICS = tuple(key for key in METRICS if key != 'success_rate')


def _suppressed(value):
    return isinstance(value, str) and value.startswith('Suppressed')


def _hide(row, key):
    if _suppressed(row[key]):
        return False
    row[key] = 'Suppressed (complementary)'
    return True


def _coverage(rows, student_counts):
    # A student can belong to several classes/programs. Do not count them twice
    # when deciding whether a recoverable sum covers at least five people.
    if all(hasattr(row, 'students') for row in rows):
        return len(set().union(*(row.students for row in rows)))
    # Plain/serialized reports lack overlap evidence: the largest original count
    # is a safe lower bound. Never infer five people by adding overlapping counts.
    return max((student_counts[id(row)] for row in rows), default=0)


def _linked(row):
    changed = False
    # A suppressed rate must not be recomputed from its numerator/denominator.
    if _suppressed(row['success_rate']) and not any(
            _suppressed(row[key]) for key in ('completed_jobs', 'failed_jobs')):
        changed |= _hide(row, 'completed_jobs')
    # jobs = completed + failed + other statuses. The latter is not published;
    # protect the identity even when all jobs happen to be terminal.
    if sum(_suppressed(row[key]) for key in LINKED_COUNTS) == 1:
        target = next(key for key in LINKED_COUNTS if not _suppressed(row[key]))
        changed |= _hide(row, target)
    # A visible zero denominator component would still reveal a hidden rate as
    # exactly 0% or 100%, regardless of the hidden positive component's size.
    for key, other in (('completed_jobs', 'failed_jobs'), ('failed_jobs', 'completed_jobs')):
        if _suppressed(row[key]) and row[other] == 0:
            changed |= _hide(row, other)
    if any(_suppressed(row[key]) for key in ('completed_jobs', 'failed_jobs')):
        changed |= _hide(row, 'success_rate')
    # Unassigned's job count is also published under unassigned_jobs. This is
    # an alias, not a link between unassigned counts and every program status.
    if 'cost_center_id' in row and row.get('id') is None:
        if any(_suppressed(row[key]) for key in ('jobs', 'unassigned_jobs')):
            for key in ('jobs', 'unassigned_jobs'):
                changed |= _hide(row, key)
    return changed


def _protect(total, children, key, student_counts):
    hidden = [row for row in children if _suppressed(row[key])]
    if hidden and isinstance(total[key], (int, float)):
        if len(hidden) == 1 or _coverage(hidden, student_counts) < 5:
            # Prefer the total so normal-sized class detail survives.
            return _hide(total, key)
    elif not hidden and _suppressed(total[key]):
        candidates = [row for row in children if isinstance(row[key], (int, float))]
        if candidates:
            return _hide(min(candidates, key=lambda row: row[key]), key)
    return False


def _complementary(rows, families, student_counts, disclosure):
    # Suppression only grows. Repeat across both hierarchy levels AND the flat
    # summary/leaf equation: hidden program rows alone do not protect B + D.
    changed = True
    while changed:
        changed = False
        for row in rows:
            changed |= _linked(row)
        for total, children in families:
            if not children:
                continue
            for key in ADDITIVE_METRICS:
                changed |= _protect(total, children, key, student_counts)
        changed |= disclosure.protect(ADDITIVE_METRICS, _suppressed, _hide,
                                      lambda cohort: _coverage(cohort, student_counts))


def safe_export(report):
    # Never mutate the manager's JSON view. Small cohorts suppress all metrics;
    # individual small cells are suppressed even in larger cohorts.
    report = deepcopy(report)
    rows = [report['summary'], *report['programs'], *report['classes']]
    student_counts = {id(row): row['students_served'] for row in rows}
    disclosure = DisclosureSystem(report, rows)
    for row in rows:
        non_concentrators = row['participants'] - row['concentrators']
        small = 0 < row['students_served'] < 5
        for key in METRICS:
            value = row[key]
            if small or (isinstance(value, (int, float)) and 0 < value < 5):
                row[key] = 'Suppressed (<5)'
        # Seed the fixed-point hierarchy checks using the original marks, even
        # when primary suppression has already hidden the participant count.
        if 0 < non_concentrators < 5:
            _hide(row, 'concentrators')
    families = [(report['summary'], report['programs']), (report['summary'], report['classes'])]
    families += [(program, [row for row in report['classes'] if row['cost_center_id'] == program['id']])
                 for program in report['programs']]
    _complementary(rows, families, student_counts, disclosure)
    report['notice'] += (' Nonzero cells below 5 and metrics for cohorts of 1-4 students are suppressed; zero is not a small cell.'
        ' Linked counts, rates and complementary cells or totals are also suppressed to prevent reconstruction.')
    return report


def label(report, key):
    return report.get('metric_labels', {}).get(key, key.replace('_', ' '))


def _cell(value):
    if value is None:
        return 'Unknown'
    value = str(value)
    # Spreadsheet formula injection can start after leading whitespace.
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n')) else value


def csv_report(report):
    report = safe_export(report)
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(('ODIN program report', report['template'], report['notice']))
    for key, value in report['filters'].items():
        writer.writerow((key, _cell(value)))
    for key, value in report['basis'].items():
        writer.writerow((key + '_basis', value))
    writer.writerow(('currency', _cell(report['currency'])))
    writer.writerow(('scope', 'program', 'class', 'term', *(label(report, k) for k in METRICS)))
    writer.writerow(('Total', '', '', '', *(_cell(report['summary'][k]) for k in METRICS)))
    for row in report['programs']:
        writer.writerow(('Program', _cell(row['program']), '', '', *(_cell(row[k]) for k in METRICS)))
    for row in report['classes']:
        writer.writerow(('Unassigned' if row['id'] is None else 'Class',
            *(_cell(row[k]) for k in ('program', 'title', 'term')), *(_cell(row[k]) for k in METRICS)))
    return output.getvalue().encode('utf-8-sig')


def pdf_report(report):
    report = safe_export(report)
    from fpdf import FPDF
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font('Helvetica', size=11)
    def line(value):
        # Built-in PDF font is Latin-1. Explicit '?' replacement keeps exports
        # available for Unicode names; JSON/CSV preserve original characters.
        safe = str(value).encode('latin-1', 'replace').decode('latin-1')
        pdf.multi_cell(0, 6, safe, new_x='LMARGIN', new_y='NEXT')
    line('ODIN funding and program report - ' + report['template'])
    line(report['notice'])
    line('Generated: ' + report['generated_at'])
    line('Currency: ' + str(report['currency'] or 'Unknown'))
    line('Window: ' + report['filters']['start_date'] + ' through ' + report['filters']['end_date'])
    for key, value in report['basis'].items():
        line(label(report, key) + ': ' + value)
    for title, row in [('Total (distinct students)', report['summary'])] + [(r['program'], r) for r in report['programs']] + [
            (r['program'] + ' / ' + r['title'] + ' / ' + r['term'], r) for r in report['classes']]:
        pdf.ln(3)
        line(title)
        for key in METRICS:
            line(label(report, key) + ': ' + str(row[key] if row[key] is not None else 'Unknown'))
    return bytes(pdf.output())


def email_report(report):
    report = safe_export(report)
    def val(v):
        return escape(str(v if v is not None else 'Unknown'))
    rows = ''.join('<tr><td>' + val(label(report, key)) + '</td><td>' + val(report['summary'][key]) + '</td></tr>' for key in METRICS)
    classes = ''.join('<li>' + val(r['program']) + ' / ' + val(r['title']) + ' / ' + val(r['term']) + ': ' +
        ', '.join(val(label(report, k)) + '=' + val(r[k]) for k in METRICS) + '</li>' for r in report['classes'])
    programs = ''.join('<li>' + val(r['program']) + ': ' + ', '.join(val(label(report, k)) + '=' + val(r[k]) for k in METRICS) + '</li>' for r in report['programs'])
    return '<h2>ODIN funding and program report</h2><p>' + val(report['notice']) + '</p><p>' + \
        val(report['filters']['start_date']) + ' through ' + val(report['filters']['end_date']) + \
        '; currency: ' + val(report['currency']) + '</p><table>' + rows + '</table><h3>Programs</h3><ul>' + programs + '</ul><h3>Classes</h3><ul>' + classes + \
        '</ul><p>' + '<br>'.join(val(k) + ': ' + val(v) for k, v in report['basis'].items()) + '</p>'
