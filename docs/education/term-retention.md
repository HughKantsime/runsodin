# End-of-term retention and graduation cleanup

D5 is available with a valid Education or Enterprise license carrying
`term_retention` and `education_workflows`. Open **Education → Term retention**
using an interactive school administrator account with **Settings: edit**
permission. Platform administrators can use the API with an explicit `org_id`.
Students, instructors without the administrator role, scoped API tokens and the
legacy global API key cannot administer retention.

## Policy and execution

Create a named term for one existing class/club (Education cost center). Supply
UTC start and exclusive end timestamps. A submission belongs to the term when
its creation time is inside that window. Policies do not depend on the parallel
D2 classes schema. Different classes can have different policies for the same term.
Policies with no cleanup receipts can be deleted with an audit record and replaced; overlapping windows for the same class are rejected.
Review the dates and delays before saving. No policy is created automatically.

For each of files, timelapses, thumbnails and portfolio visibility, blank/null
means keep. An integer N means eligible at **term end + N days**, including zero.
There is no unattended deletion job. A school administrator decides when to
execute each due batch after reviewing a dry run. This makes the destructive
operation and its selected content reviewable without a service credential.

Preview is read-only with respect to content; it writes an audit event. It lists
selected submission IDs, due categories, eligible departed usernames and account IDs and
blocked reasons. Large terms preview 500 submissions at a time. Purge processes at most 25 submissions per request and stops admitting work after ten seconds, returning a resume cursor. A single in-progress media operation must finish or refuse before returning. Use the
next-page control, including when the first page has already been cleaned up.
Upload hashing happens outside write transactions; inode, size and modification time are rechecked before unlinking. Due categories share short write transactions, and PostgreSQL locks only the affected rows. Timelapse retirement runs after upload verification and outside those transactions.
The fingerprint and page cursor are required when executing; stale previews
return 409. Work and content identity are rechecked after each committed intent.

Purge retains a per-submission/category receipt, records started/completed or
blocked audit events, and processes items independently. A blocked item does not
pretend to have been deleted. Run another preview and retry after resolving it.
An interruption after unlinking a file leaves a pending intent; retry finishes
its accounting and completion record without releasing storage twice.

Active jobs, unresolved printer claims, shared models/files, unconfirmed archive
ownership and changed content are refused. Files must be within the configured
`EDUCATION_UPLOAD_ROOT`, in the school's directory, and match the upload SHA-256.
Symlink paths, hardlinks and changed bytes are refused. Storage-account drift
requires reconciliation. The implementation never recursively deletes an upload
folder. Unresolved legacy timelapses are reported as blocked; the existing sealed
retirement service verifies ownership and removes owned videos, frames and
associated publication content. Its ordinary refusal/recovery rules still apply.

## Exactly what is removed and retained

| Choice | Removed or changed | Retained |
|---|---|---|
| Files | Owned Education upload bytes; extracted mesh data; charged storage released once | File and upload rows, original filename/path metadata, SHA-256, compatibility metadata and submission/job relationships |
| Thumbnails | Embedded preview fields in the print file, model and associated print archives; model thumbnail URL reference | Other metadata. A thumbnail embedded inside a retained 3MF remains inside that file. Remote or separately managed image bytes are not deleted by removing a URL reference. |
| Timelapses | Media proven owned by the existing sealed retirement workflow; current media rows are retired | Immutable ownership, publication and retirement evidence and purge audit trail |
| Portfolio visibility | Submission moves to the retained archive; model is hidden from the active library | School archive, submission, review/approval history and model metadata. There is no separate D2 portfolio table in this checkout. This is archival visibility, not deletion of school records. |
| Graduation | Existing GDPR account erasure: account becomes a disabled tombstone, credentials/sessions/tokens and supported identity records are removed or scrubbed | Stable numeric account references, school memberships, submissions, decisions, jobs, accounting and audit history |

Every cleanup keeps Education submissions, membership and grant history,
review/approval identities by stable ID, lifecycle revisions, job and print
outcomes, material/cost/quota totals, archive records and both general and
Education audit trails. User-supplied filenames, titles, notes, reasons and other
school-record free text can remain. Content cleanup is not a global search for
personal information. Neither D5 nor existing erasure guarantees that arbitrary
personal text embedded in those retained records is anonymized.

Backups, exported/downloaded copies, printer SD cards, external storage and
unrelated model revisions are outside this cleanup. Their retention is a separate
school responsibility. Restoring an older backup can restore content previously
purged. Run the reviewed policy again after restoration.

## Bulk graduation

Deactivate departed student accounts and revoke their active class grants first.
The preview offers only viewer/student accounts associated with this term by a
submission or a student grant overlapping its dates. Accounts must still belong
to this school, be inactive, and have no active grant in any school or nonterminal
job. Administrators/operators and accounts transferred to another organization
are excluded; use the existing individual GDPR workflow where appropriate.

Select up to 100 account IDs and confirm. The term must have ended. The complete
selection is validated before any erasure, then eligibility is rechecked for each
account. Existing GDPR erasure commits per account, so the result explicitly
reports partial success using `erasure_incomplete` only when a durable recovery
row remains after rollback. Other failures report `blocked` with a reason code.
Original identifiers are retained in a private recovery row until all erasure
chunks finish, then removed. The administrator's retention page lists pending
erasures for currently eligible, resumable students in this school, even without
selecting a term. Other accounts must use the individual GDPR erasure workflow.
Use **Resume erasure**
after resolving the failure; eligibility is checked again under the account lock.
The list shows account numbers and current usernames, never private recovery
identifiers. Fully erased accounts disappear from the list and graduation choices.
Failures before the first durable erasure commit leave the account unchanged;
select those accounts again from a new graduation preview.
Anonymization does not implicitly purge content or alter a term's keep choices.

GDPR exports now include the requester's term policies (if they created them) and
cleanup receipts for their submissions. GDPR erasure clears policy creator
attribution. Receipts keep stable submission references for school audits.
Policies and receipts restrict hard deletion of their parent school/history.

## API and database

Both `/api` and `/api/v1` expose:

- `GET /education/term-retention`: policies in the authorized school.
- `POST /education/term-retention`: create a policy.
- `DELETE /education/term-retention/{id}`: delete a policy without receipts, with an audit record.
- `GET /education/term-retention/{id}/dry-run?after_id=0`: preview a page.
- `POST /education/term-retention/{id}/purge`: `{fingerprint, after_id}`.
- `POST /education/term-retention/{id}/anonymize`: `{fingerprint, after_id, user_ids}`.
- `POST /education/term-retention/erasures/{user_id}/resume`: resume an eligible pending account in this school.

The policy list includes `pending_erasures` for administrators. A PostgreSQL
student-row lock timeout returns HTTP 409 with `student_busy_retry`; retry the
request once the account is no longer busy. Purge hash timeouts return `blocked`
with `hash_budget_exhausted` and advance the cursor only for the first processed
item, which had the full request budget. Later items that exhaust the remaining
budget leave the cursor unchanged so the next request revisits them with a fresh
budget. A vanished
submission returns `submission_gone` and cleanup continues. Completed receipts
are rechecked before failure audit rows, so a losing concurrent purge does not
report categories already completed by the winner as blocked.

Migration `python:053-term-retention` adds `education_term_policies`,
`education_term_cleanup` and temporary `education_erasure_pending` recovery rows. All three are in the post027 registry and backup inventory.
No PostgreSQL functions or triggers are introduced. `preserved046` freezes the
previously released schema for historical PostgreSQL restore staging; no existing
frozen package or released ledger entry is changed.

## Privacy wording for approval — NEEDS SHANE

Proposed text (review round 1):

> When a school runs term cleanup, ODIN deletes the selected students' uploaded print files, the timelapse videos it can prove belong to those submissions, and stored preview images, and hides that work from the active view. Graduation erasure disables a departed student's account and replaces or removes its username, email, password, sign-in links, sessions, tokens and Classroom identity, leaving a numbered placeholder. The school keeps its records under that number: submissions, approvals and rejections, jobs, print outcomes, material use, costs, quotas, archive entries and audit logs. Those records can still contain text people typed, such as file names, titles, notes and review comments. Cleanup does not reach backups, downloaded or exported copies, printer storage, camera detection frames, or timelapses ODIN cannot attribute to a submission.

Shane must approve this wording and the school's chosen retention periods before
release. This is an implementation description, not a claim of legal compliance.

## Pricing-page handoff

The public pricing page is in the separate `odin-site` checkout and was not edited
by this ODIN-only task. Suggested Education lead: **Classroom governance, print
review and end-of-term content cleanup**. Enterprise inherits these Education
capabilities alongside its own shipped audit/export controls. Do not present
unlimited capacity or air-gap operation as Enterprise-only differentiators, and
do not advertise the other parallel tier-value features until they are released.
