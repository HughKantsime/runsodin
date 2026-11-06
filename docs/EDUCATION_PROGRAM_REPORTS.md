# Funding and program reports (D3)

Education and Enterprise licenses include `program_reports`. The existing
`education_workflows` entitlement is also required. D2 classes remain the source
of attribution. No paid service or new account is used.

In Education → Classes, report an existing program without creating a class,
or select a class you manage. Funding and program reports can cover that class, its program (the existing Education cost center), or its
term across the classes you manage. Choose an inclusive school-day date window, up to
367 days. The organization `timezone` setting uses an IANA zone, with UTC fallback
labelled in every report. Generate a summary or download CSV/PDF. The reports contain class
names and aggregates, not student names, emails, file names, grades or feedback.
CSV retains Unicode and escapes spreadsheet formulas; PDF replaces characters
unsupported by its built-in font with `?`.

## Definitions

- **Students served:** distinct students with submissions in
  the selected submission-created date window and a proved retained ownership
  graph. All statuses count. A student in several classes counts once in the
  total. Class totals therefore need not sum to the distinct student total.
- **Jobs:** the same proved jobs. Program totals include unassigned submissions,
  counted separately as `unassigned_jobs`. Class/term filters include only D2's
  explicitly assignment-linked submissions. Programs without classes still have
  program totals in the web UI, API and scheduled reports. Unproved history is counted separately
  and never contributes student, material, time or cost totals.
- **Print hours:** sum of `print_archives.actual_duration_seconds` across all
  attempts of completed and failed jobs, including failed attempts before reprints.
  Missing duration is unknown. Partial jobs contribute known hours and also count
  as unknown-hours jobs; the final job interval is not used.
- **Material grams:** archive-recorded measured grams, or the file estimate when
  the archive used it, for all attempts of those terminal jobs. Multiple archive
  attempts do not multiply job/student/cost counts. A partially measured
  job contributes its known grams and is also counted as unknown material.
- **Cost:** summed terminal-job estimates in the school's current configured
  currency, not actual expenditure, historical currency conversion or a funding
  reimbursement claim. Unpriced jobs are counted separately. No known samples
  yields `Unknown`, not zero.
- **Success rate:** completed / (completed + failed). Cancelled, rejected and
  pending work is excluded from the denominator. No terminal jobs means unknown.
- **Instructor-entered teaching hours:** the teaching-effort ledger entered by a tenant admin for
  an active class instructor and activity date. One entry per class/instructor/day;
  a revision-safe correction replaces it, with zero cancelling an erroneous value.
  No recorded entries yields zero. The admin attests approval; ODIN does not infer
  labor hours from approval clicks or printing duration.
- **Print hours approved by instructors:** archive attempt hours for proved jobs
  with retained `submission.approve` school audit evidence. This includes
  instructor-authorized automatic class rules. It is separate from teaching
  effort; missing archive duration is unknown, with a separate partial-job count.
- **Participants/concentrators:** explicit instructor class marks, with a
  concentrator also marked as participant. These apply to the whole selected
  class/term, independent of the print date window. They include retained marks
  after roster changes. Orphaned marks remain visible and both flags can be cleared
  without an active student grant. Totals deduplicate people across selected
  classes. Without a term filter the report labels them **all-time marks**.

The generic report ships independently of the **draft Perkins V planning
worksheet**. The organization setting `program_reports_perkins_enabled` is off
by default; admins can enable it in organization settings. Perkins requests require
a term or class (422 otherwise), and are refused while disabled (403). Customer
wording says “field definitions not yet confirmed.” That worksheet presents participant/concentrator counts alongside
program metrics, and clearly says it is not an official filing. **NEEDS SHANE:
confirm the exact fields CTEC reports and approve the worksheet before release.**
No statutory definition or eligibility is inferred by ODIN.

## Authority, audit and privacy

The existing `education.classes_read` and `education.classes_manage` matrix
keys cover the new routes. A live cost-center manager or tenant admin must also
pass D2's scope checks. Students cannot read class aggregates or classmates'
marks. Read/write Education API token scopes are required; a global API key is
insufficient. All outputs use `private, no-store`.

Mark changes and approved-hour corrections record typed before/after values in
immutable school audit receipts only; the general audit stores identifiers and
revision, not mark values. CSV/PDF downloads and schedule configuration/runs
are audited. JSON report and class-record reads write no audit rows. Downloads
require a live school admin or manager. CSV/PDF/email suppress nonzero numeric
cells below 5 and all metrics for cohorts of 1–4 students; zero alone is not small.
Each unfiltered program includes an explicit **Unassigned** detail row with every
metric. Class/term filters exclude unassigned work. Jobs and completed/failed
counts receive complementary protection for their status identity; success rate
is hidden whenever completed or failed is hidden. Unknown-measurement counts and
unassigned-job counts do not blanket-suppress unrelated status metrics.
Subtracting completed and failed jobs from jobs can reveal a small count of
other statuses, such as cancelled or pending. This is an accepted job-level
count, not a count of students; it does not trigger additional suppression.
When participants minus concentrators is 1–4, concentrators are hidden, with
complementary protection through the program and summary groups.
Complementary cells or totals are hidden until the group equations cannot recover
an unsafe hidden cohort. Exact rational elimination checks all determined cohort
combinations across program/class-plus-Unassigned, summary/program and
summary/all-detail rows. Participants, concentrators and the virtual difference
between them use the actual inferred people count (0 or at least 5), independent
of print submitters. The virtual metric includes separate reconstructions of
participants and concentrators from different visible rows. Unassigned's marks
and teaching hours are known zeros, even when the displayed cell is suppressed.
Job and other activity metrics retain the distinct-student coverage rule. Original
cohort evidence handles overlapping students without exporting their identities;
a serialized report without that evidence uses a conservative count bound.
Local checks prefer hiding totals to preserve normal-sized class detail; remaining
unsafe equations hide the smallest involved visible cell, with stable report-order
tie breaking, and all checks repeat until no further suppression is needed.
Complementary suppression is labelled separately and does not
imply a value below 5. The authorized manager JSON view retains exact values.
For reports shared outside the school, use fixed, non-overlapping periods.
Differences between overlapping report runs can isolate small groups; suppression
within one report does not protect against comparisons across runs.
Suppression never alters the source report. GDPR exports include one's own classification and instructor effort;
a mark author/approver receives only their reviewer role metadata, not another
person's classifications/hours. Erasure removes approver identity and retains
numeric school records under ODIN's existing numbered user placeholder. Existing
class/term text scrubbing and school audit erasure still apply.

Program email schedules are deactivated when their original owner or latest editor
is erased, disabled, demoted, or ceases to be a platform admin.
That person's recipient mailbox is removed from other program schedules; an
empty recipient list disables the schedule. Exported/downloaded/emailed copies
and backups are outside erasure. Aggregate reports are not stored as snapshots.

## Scheduled email

A platform administrator uses Settings → Scheduled Reports → `program report`.
School organization ID is required; cost-center ID, class ID and term are optional.
Use the IDs returned by the class/cost-center APIs. Select 1–367 previous complete
school days in the configured timezone (UTC fallback); the date window rolls at execution. Select generic or draft Perkins.
Existing SMTP configuration, destination policy, frequency and delivery machinery
are reused. Email is HTML with the same aggregate definitions and class detail;
CSV and PDF remain on-demand downloads.

Recipient mailboxes must belong to active admins or active cost-center managers
of the selected school. Each non-admin recipient must manage the selected program
(resolved from the class when selected); manager recipients require an explicit
program or class filter. School-wide and term-only schedules require school-admin
recipients. Creation and edits validate the recipients; every send
rechecks them, including after rendering. License and live platform-admin authority
of both the original owner and last configuring actor are rechecked at execution. A missing school, invalid scope, disabled/demoted creator, lapsed
program entitlement, missing Education entitlement, or unconfigured SMTP prevents
delivery. Pause/delete remain available after a license lapse. This uses the
existing runner's retry/timing behavior; it does not promise exactly-once email.
No email was sent during implementation tests.

## API and migration

Both `/api` and `/api/v1` expose:

- `GET /education/program-reports`: `start_date`, `end_date`, optional
  `cost_center_id`, `class_id`, `term`, `template=generic|perkins`,
  `format=json|csv|pdf`.
- `GET /education/program-reports/options`: school timezone and Perkins availability.
- `GET /education/program-reports/classes/{class_id}`: class marks, latest
  100 instructor-hour entries, and whether the actor may approve hours. Optional
  `activity_date` selects a date for correcting older entries.
- `PUT /education/program-reports/classes/{class_id}/marks/{user_id}`:
  `participant`, `concentrator`, `revision` (zero for first save).
- `PUT /education/program-reports/classes/{class_id}/instructor-hours`:
  `instructor_id`, `activity_date`, `hours` (0–24), `revision` (zero for first save).

Reports refuse more than 1,000 selected classes or 65,536 submission/mark rows;
use a narrower scope instead of accepting silently truncated totals.

Migration `050_education_program_reports.py` owns `education_program_marks` and
`education_instructor_hours`. It follows D2's exact shape/FK validation pattern;
PostgreSQL temporary shape tables omit permanent FKs and validate FK targets
separately. It runs after dedicated 037 and D2 049, outside the post027 loop.
It refuses missing `education_memberships` or `education_classes` before creating
any tables. There are no new PostgreSQL functions or triggers. The migration is registered
in the ledger and raw schema/backup inventory. Release 2.0.45 assigns this migration number 050, after D2 classes at 049.

PostgreSQL execution and independent Claude review remain required at review.
The external pricing site is a separate checkout and is not modified by D3.
Suggested Education/Enterprise copy: “Funding and program reports by class and
term, CSV/PDF exports, scheduled email, and instructor-marked CTE participation.”
