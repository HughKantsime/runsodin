# Classes and assignments (D2)

Education and Enterprise include the `classes` feature. A valid signed
`education_workflows` entitlement is also required. Community and Pro requests
receive the existing feature-license refusal. GDPR export and erasure still work
when the license expires. No external account or paid service is used.

## School workflow

1. An administrator configures a cost center, its students, its managers and its
   authorized printers in Education → Cost centers & rosters. Managers are the
   instructors, regardless of their application role.
2. An instructor or school administrator opens Classes & assignments and creates
   a class with a title, term and existing cost center. Multiple terms can use a
   center. **The roster is live**, not a term snapshot: changing center grants
   changes access to every class using it. Use separate centers for distinct
   cohorts. Students see instructors and their own membership, not classmates.
3. The instructor creates an assignment: title, brief, timezone-aware due date,
   maximum estimated minutes and grams per print, allowed filament materials,
   and number of submissions per student. Blank limits are unrestricted. All
   submissions that reached approval consume this quota, including those that later fail.
   Pending or rejected-before-approval work does not consume it; approval rechecks
   the limit under the assignment lock.
4. A student uploads with the existing Submit print workflow to that cost center,
   then explicitly links the pending print to an assignment. The upload remains
   unassigned until linking succeeds. Uploading alone does not enroll it in an
   assignment. Linking rejects overdue/closed assignments, stale or reviewed
   submissions, wrong owners/centers, missing grants, unknown constrained estimates,
   material violations and exhausted quotas. The assignment row serializes quota
   admission; an existing link is idempotent and cannot be moved to another assignment.
5. Instructors give feedback, a freeform grade (100 characters), and an assessment
   status: submitted, reviewed, needs revision, or complete. These are independent
   of print outcome; grading never changes a job. Revision conflicts require reload.
6. My portfolio shows the student's own submissions, including unassigned and
   archived work, with feedback, grades, thumbnails when present and timelapses
   when linked to an owned print archive. Media is still authorized at playback.
   Missing thumbnails/recordings are shown as unavailable, never fabricated.

Archiving a class or closing an assignment prevents new links. It retains records
and permits assessment of existing work. Class updates do not modify center grants.
School accounts perform these operations; a platform superadmin must use an account
in the school, matching existing Education review policy.

## Automatic approval and safety

Auto-approval is off by default. It requires both a time threshold and a grams
threshold plus an explicit list of currently entitled printers. Printers are evaluated
in deterministic ID order until one approves. If all are unavailable/incompatible, estimates
are unknown or outside the thresholds, the configuring instructor is inactive or
has lost manager access, or the class/assignment is closed, the submission remains
in manual review. Changing the rule records the configuring instructor anew.

The existing `approve_submission` service performs approval, including active
printer entitlement, compatibility, revision and lifecycle checks, audit records
and notifications. Dispatch retains its existing safety checks. Assignment limits
are also checked by ordinary and explicit-AMS review against the actual selected
print file, so a changed payload cannot bypass them. Unknown measurements fail a
configured bound. Limits are slicer estimates, not a guarantee of actual consumption.
Changes to an assignment do not retroactively cancel previously approved jobs.

Linking commits before the optional approval attempt. A crash/failure cannot lose
an accepted link. Repeating the same link returns its existing association and
never retries automatic approval; an instructor can review it through the ordinary
approval path. The response distinguishes approved, manual review, already reviewed and
review unavailable. No background retry daemon or new hardware I/O is introduced.

## API and permissions

Both `/api` and `/api/v1` expose the same routes below, prefixed with
`/education/classes`. All require `classes`, Education principal authentication,
and the relevant `read:education` or `write:education` token scope. The permission
matrix is an additional restriction; it cannot grant a school role.

| Method and path | Permission | School authority |
| --- | --- | --- |
| GET root | education.classes_read | Current center student/manager, or school admin |
| POST root | education.classes_manage | Center manager or school admin |
| PUT /{class_id} | education.classes_manage | Center manager or school admin |
| GET /{class_id}/roster | education.classes_read | Class member; students have a private roster projection |
| GET /{class_id}/assignments | education.classes_read | Class member or school admin |
| POST /{class_id}/assignments | education.classes_manage | Center manager or school admin |
| PUT /{class_id}/assignments/{assignment_id} | education.classes_manage | Center manager or school admin |
| GET /assignments/{assignment_id}/submissions | education.classes_read | Own submissions, or class instructor/admin |
| POST /assignments/{assignment_id}/submissions | education.classes_submit | Owner with a live student grant |
| PUT /assignments/{assignment_id}/submissions/{submission_id} | education.classes_assess | Class instructor/admin |
| GET /portfolio | education.classes_read | Own submissions only |
| GET /portfolio/{submission_id}/thumbnail | education.classes_read | Owner or linked assignment instructor/admin; existing file authority also checked |

Lists use bounded `limit` and `offset`, with `next_offset`. Writes use optimistic
`revision` for edits, assessments and attachment. Creation returns a UUID and 201.
No deletion endpoints exist. A lost response during class/assignment creation should
be resolved by reloading the list before submitting another creation.

## Audit and privacy

Class/assignment mutations append immutable `education_audit_events` and ordinary
audit entries in the same transaction. Audit events store resource IDs, actions
and revisions, **not** titles, briefs, feedback, grades or cached personal responses.
Reads and thumbnail downloads do not write audit rows. Automatic
approval records the rule's instructor as its authorizer and marks the approver
as automatic in both the class event and ordinary submission approval event.

GDPR exports add the user's associated classes, authored assignments, their own
assessments and assessment revisions, and their D2 school audit actions. Teacher
exports contain only identifiers and reviewer role for students' assessments and
revision history. Erasure clears feedback and grades only on the student's own
work and history. For teachers it retains marks, nulls their reviewer IDs,
disables their auto-approval rules, and scrubs their known identifiers from class,
assignment and assessment free text. Existing erasure revokes their center grants
and anonymizes/disables the account. No mutable assessment copy is stored in an
idempotency cache or school audit record. D2 responses carry `private, no-store`,
which also makes the generic idempotency middleware retain only a non-replayable
receipt, not the response body. An inactive or erased student cannot receive new
assessment text. Each assessment save appends who, when, and old/new grade,
feedback and learning status to `education_assessment_revisions`. Ordinary edits
never rewrite or delete earlier revisions; GDPR text scrubbing is the explicit
exception. Grading archived cost centers remains available to active authorized
instructors. Update requests change only explicitly supplied fields.

What remains: pseudonymous user IDs, retained Education membership, class/term and
assignment structure, submission links, assessment status, timestamps/revisions,
job outcomes, and immutable school audit IDs/actions. Existing Education files and
media retention policy is unchanged; D2 does not claim to purge all historical
content. Term-based content cleanup belongs to D5. School records have RESTRICT
foreign keys and cannot be hard-deleted through user/organization deletion.

## Schema, release and integration

`python:049-education-classes` is additive, creates its own four tables, registers
in the post027 runner and backup inventory, and validates exact shapes on SQLite
and PostgreSQL. Its assessment-history function and triggers are registered in
the canonical PostgreSQL executable inventory. Released ledgers remain unchanged. `preserved046` freezes the released schema lineage ending at 046
for historical PostgreSQL archive staging. The existing native post027 restore
runner now seeds and checks a row in each D2 table.

Claude owns final independent review and the full census. Native PostgreSQL
backup/restore verification remains a release check when a disposable server is
available. Parallel feature branches must preserve migration order when merging.

Pricing-page handoff (the pricing site is outside this checkout): lead Education
with “Classes and assignments, instructor review, and student portfolios”; Enterprise
includes these Education workflows plus its own enterprise controls. Air-gapped
operation and unlimited capacity are not Enterprise-only claims. No credentials or
NEEDS SHANE configuration is required for D2 itself.
