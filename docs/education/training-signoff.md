# Training before printing

Education and Enterprise include `training_signoff`. It also requires the existing
signed `education_workflows` entitlement. Community, Pro, expired licenses and licenses
that explicitly omit Education workflows receive the existing feature-not-licensed refusal.
No external account, credential or paid service is required.

## School workflow

1. Enable Education mode and configure a class under **Education → Classes & clubs**.
   Assign students, managers (instructors) and authorized printers as usual.
2. Open **Education → Training**, select the class, and choose **New training**.
3. Give it a name and description. Choose all printers authorized for that class,
   an exact `machine_type` (for example `P1S`), or the printer's configured model name.
   Model/type matching ignores case and surrounding whitespace. There is no
   separate printer-group entity: the class's existing authorized-printer set is the group.
   Tenant admins can select **School-wide training** to cover matching printers across the school.
   Only a live tenant admin can create, edit, archive or toggle a school-wide definition;
   managers of any active class a student belongs to can sign off or revoke that student's
   school-wide training. Their roster shows only their own students; tenant admins see all students.
   The editor shows matching printers and refuses an active scope matching none.
   Printer edits warn when they remove a match; class-printer removal and printer deletion
   warn when the last match disappears. Printer deletion remains HTTP 204 with JSON warnings
   in the `X-ODIN-Training-Warnings` response header, which the web app displays.
   Training cards also warn about empty scopes.
4. Require instructor sign-off, add multiple-choice questions and a pass mark, or use both.
   When both are configured, **both must be completed**. A score is the whole-number
   percentage of correct answers, rounded down. Each question has one correct choice.
5. Optionally enter an expiry in days (1–3650). Expiry starts when all requirements
   are completed; a blank value means no expiry. Quiz and instructor steps may occur
   in either order. A completed training cannot be renewed by repeating a request.
6. Students see their own requirements and can take quizzes. Instructors use
   **View student sign-offs** to see status, score, expiry and sign off or revoke a student.
   Three consecutive quiz failures impose a 15-minute cooldown. Attempt counts and
   cooldowns appear in the roster; responses never contain per-question correctness.
   Once a quiz is passed, it cannot be retaken while awaiting instructor sign-off.
   Retained evidence can still be revoked after a student leaves (by a tenant admin for
   school-wide training); new completion requires an active student grant.

Changing quiz content, pass mark, instructor requirement or scope creates a new
revision and invalidates earlier completion evidence. The editor warns before saving.
Every definition save advances a separate `edit_version`; stale editors receive HTTP 409
without overwriting newer text. Name, description, expiry and active-state edits preserve completion evidence; expiry
changes apply to future completions and do not rewrite existing expiry timestamps. Archiving removes that requirement from admission without deleting
history. Revocation or expiry requires a fresh quiz and fresh instructor sign-off when
both were configured. Retrying the same command returns its original result and does not
create a second audit event or extend expiry.

## Admission and authority

All matching active trainings from every class in the school sharing the printer must
be complete, along with school-wide requirements. The selected class cannot bypass
another class’s safety requirement. Students can see and complete those shared requirements. If none match, existing behavior is
unchanged. Students upload to a class, not to a selected printer: admission requires
training for at least one currently authorized class printer. Approval checks the exact
instructor-selected printer and the **student's** training, in addition to existing file
compatibility checks. Training is rechecked at upload commit, scheduling, dispatch
reservation, and the final dispatch lock. A training revoked or expired while a job waits
cannot be bypassed by its earlier approval. Existing queue reconciliation sends work whose
authority changed back for review; it does not silently choose a different printer.

Refusals use HTTP 403 with `error.code = training_required`, a human-readable list of names,
and `error.required_trainings` containing IDs, names and statuses. Existing uploaded work
and history remain intact. Requirements remain enforced for existing work even when the
training license is removed; downgrading is not a bypass.

A class manager or school admin bypasses **their own** training requirements. A teacher
approving a student's job does not bypass the student's requirements. A generic Operator
without the class's manager grant is not an instructor. Platform Super Admin accounts
cannot acquire Education grants; use a school-level account for training administration.

Every route uses the existing Education principal and `read:education`/`write:education`
token rules, plus `training_signoff` and an explicit permissions-matrix key:

| Key | Routes | Additional authority |
| --- | --- | --- |
| `education.training_read` | GET class trainings; GET training records | Active class membership; records require manager/admin |
| `education.training_take` | POST training quiz | Active student grant; own answers only |
| `education.training_manage` | POST class training; PUT definition; POST signoff/revoke | Manager of that class or school admin; school-wide definition changes require tenant admin |

All application roles are initially eligible for these keys because Education manager
grants are independent of application roles. Settings → Permissions can restrict them
further. Tenant boundaries and class authority still apply. Both `/api` and `/api/v1`
aliases are registered. Students never receive the answer key or another student's roster.

## Audit and personal data

Definition changes, quiz attempts, sign-offs, revocations and GDPR revocations write
Education audit events. Mutation results and evidence commit in the same transaction.
Assessment events retain numerical user IDs, training ID/revision, score, status, expiry,
actor ID and a bounded reason category. Quiz answers and editable definition prose are not
copied into the immutable audit or command-result history. Request hashes support retries.

The existing user export includes current training evidence and training events associated
with the user as subject or actor. Current records are limited to that user, never
students signed off by an instructor exporting their own account. It contains no quiz answer keys. GDPR erasure revokes all
the user's training evidence (preserving any existing revocation timestamp) and follows the existing account anonymization workflow.
Training records, numerical identity references and immutable school audit events remain;
profile names/emails are anonymized by the existing erasure process. Training definitions
remain class-owned content. Do not put personal information in training names/descriptions.
Hard deletion of Education users or organizations remains blocked by retained history.

## Schema and restore compatibility

Migration `python:048-training-signoff` creates `education_trainings` and
`education_training_records`, with tenant-bound foreign keys to existing centers and
retained Education memberships. It rewrites no existing data and adds no SQL functions or
triggers. Both tables are in the canonical backup inventory. Schema validation checks
column types, nullability, defaults, keys, constraints and indexes on SQLite/PostgreSQL.

`preserved046` retains the published 046 restore contract. Existing entries in
`released_post027_ledgers.json` are unchanged.
