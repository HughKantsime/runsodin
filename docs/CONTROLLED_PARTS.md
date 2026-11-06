# Controlled parts (Enterprise)

Controlled parts restrict files, jobs, names and media to explicit audiences. CUI and ITAR are labels, not certifications. `ODIN_ITAR_MODE` remains a separate outbound network policy.

## Audience and organization access

Interactive superadmins manage named controlled audiences and select individual active users. Audiences are independent of organizations. Membership does not grant organization access: both checks must pass. Superadmins must join an audience to read its work. Legacy global API keys cannot establish membership.

In a file upload result or job timeline detail, choose **Change marking**. Operators and admins need the existing model/job editing permission and must belong to the selected audience. Supply a label, audience, exact case-sensitive printer tag, retention of 1–3,650 days, and a reason. Superadmins can manage audiences in the same panel. Changes record before/after membership and flag self-additions. Education-owned work cannot be marked.

File restrictions follow aliases sharing its stored path, model variants, and jobs selecting that file. A directly marked job also restricts its selected file and reprints. Membership is read from the live database. License expiry does not expose marked content, but new marking changes and controlled dispatch require Enterprise.

Marking locks affected jobs, then files, in ID order before checking printing, paused and live dispatch claims. Source replacement requires release. A directly marked job cannot be deleted; a job merely using a marked file may be deleted without releasing that file. Organization deletion and bulk deletion refuse marked dependencies by name, before foreign-key failures.

## Printers, events and alerts

Nonmembers retain printer cards, status and pause/stop/resume controls. Job/file names are redacted. Camera and media access require audience membership. Controlled work uses authenticated MJPEG; audience is checked before frames leave. WebRTC signaling is refused for controlled work. Dispatch first retires earlier camera sessions; failure to confirm the reset refuses dispatch.

Scheduling treats controlled printer tags as hard constraints and skips refused queue heads. Final dispatch admission rechecks entitlement and tags. WebSocket completion/failure events resolve scheduled and physical job identities and refuse unresolved names. Printer telemetry retains status while redacting nested job names.

Audience members still receive their alerts. External system/org webhooks, quiet-hours webhook digests and MQTT receive generic controlled-work copy, without file/job names or free-form metadata.

## Exports and administration

Source lists and CSV exports apply controlled visibility. Analytics and next suggestions filter controlled work in SQL. Orders, products/BOM, batches, presets and timeline responses redact known fields of identified controlled resources. Matching text in unrelated resources is never rewritten. GDPR exports use the exporter's membership, including when exporting another user's records; restricted reasons and audit details are redacted.

Superadmins can see audit metadata, but controlled details remain redacted. Backups require explicit acknowledgement before an audited privileged download. The UI asks for acknowledgement when needed. Support bundles require the same acknowledgement and omit potentially sensitive logs/settings when controlled marks exist. They include diagnostic counts, not controlled rows. Downloaded backups remain the administrator's responsibility.

## Timelapse retention

Each timelapse deadline starts at the later of its completion time and the original policy marking time, plus the retention period. New prints receive the full window even when the mark is older than that window. The earliest applicable policy deadline wins. **Preview timelapse cleanup** shows due media; **Acknowledge and enable cleanup** enables retirement for the previewed policy revisions. Changing a mark resets acknowledgement.

Deleted controlled jobs retain every applicable rule, keyed by job ID, audience, deletion audit ID and rule position. Rules for the same audience preserve their original marking times, acknowledgement and clock histories. All media referencing the deleted job ID inherits these rules, including later archives and edited timelapses; the earliest effective deadline wins. After migration 055, new SQLite job IDs are never reused through the supported creation paths. IDs shared by two jobs before the upgrade stay merged: their retained history cannot be separated retroactively.

An independent worker runs bounded passes every five minutes, outside the capture loop. Failures wait until the next pass. Retirement uses the existing sealed lifetime journal and removes proved media bytes from disk; uncertainty preserves media and records a refusal. Ordinary job-history retention excludes directly marked jobs. Unmarked platform-admin legacy timelapse visibility remains available.

## API and storage

- `GET /api/controlled-parts/{files|jobs}/{id}`: effective policy, without a per-view audit write.
- `PUT /api/controlled-parts/files/{id}`: operator floor, `models.edit`, `write:models` token scope.
- `PUT /api/controlled-parts/jobs/{id}`: operator floor, `jobs.edit`, `write:jobs` token scope.
- `GET/POST /api/controlled-parts/audiences`; `GET/PUT/DELETE /api/controlled-parts/audiences/{id}`: interactive superadmin audience administration, with the `settings.edit` matrix key. `GET /api/controlled-parts/audiences/eligible` exposes only the caller’s active memberships for marking selection.
- `GET /api/controlled-parts/retention/preview`; `POST /api/controlled-parts/retention/acknowledge`: preview and enable cleanup; Enterprise licensing applies to both. Acknowledgement requires an interactive session, `jobs.edit`, and the same CSRF header.
- Backup/support downloads acknowledge through `POST /api/backups/{filename}/acknowledge` and `POST /api/admin/support-bundle/acknowledge`. These require an interactive superadmin, `settings.edit`, and `X-ODIN-Controlled-Ack: true` with same-origin validation. GET query parameters cannot acknowledge. Restore/stage use POST with the same header and `acknowledge_controlled=true`.

Example mark:

```json
{"controlled":true,"label":"CUI","audience_id":12,"printer_tag":"Restricted room","retention_days":30,"reason":"Customer handling instruction"}
```

Migration `python:054-controlled-parts` adds controlled audiences, members, files and jobs. Policies include mark/preview timestamps. Audit records retain changes. GDPR exports include membership identifiers; erasure removes memberships, clears authors and scrubs reasons while retaining protection. Numeric migration slots must be coordinated before merging parallel tier work.

License representation/device/quota helpers are split into separate files; Enterprise-only tier decisions remain in `license_manager.py`. No release, deployment or pricing-site changes are included in this checkout.

## Review round 2 behavior

Editing labels, tags, reasons or extending retention preserves the original mark clock and acknowledgement. Audience switches preserve the original clock and require another preview, so switching back and forth cannot extend retention. Shortening retains the original marking time and requires another preview. Policy history stores previous/new days and change times. For each already completed timelapse, the new deadline is the earlier of its previous deadline and the later of its shortened deadline or seven days from the change; shortening never postpones a purge. Re-marking within 30 days of release preserves the original clock. Extensions are explicitly recorded in the audit details; they cannot undo an earlier shortening for existing media by cycling retention values. Prints completed after a change receive the current full retention window. The earliest effective deadline across linked policies governs deletion. Due media awaiting acknowledgement raises a persistent admin notice.

Attributable legacy timelapses without ownership anchors can be purged under the shared media writer guard when a surviving controlled job proves the policy. Unsafe paths, links, ambiguous filenames and retained lifetime history are refused. The operation removes the video and legacy frame directory; interrupted missing-file cleanup can resume. Unattributable legacy media remains outside this automatic purge.

Idempotency replays resolve the current actor and redact again. Audit searches exclude controlled rows for nonmembers, while unfiltered list/CSV exports retain redacted metadata. Deleting a job that used a marked file retains its audiences in a tombstone; audit details are redacted at read time for nonmembers. Education upload path reservation, finalization and graph creation refuse marked aliases; cleanup also preserves protected shared bytes.

## Review round 3 behavior

Printer names stay controlled after completion while its latest physical print resolves to a controlled job/file. Cameras and printer-only alert recipients are restricted only during active controlled work or a dispatch in flight. A new physical print replaces that binding. Printer-side repeats matching the printer's authenticated dispatch receipt inherit the controlled job binding, including when only a Bambu subtask name or G-code filename is observed. The binding grants no new dispatch or accounting authority. Live status, printer lists and WebSocket telemetry redact `gcode_file`, `subtask_name` and `job_name` for nonmembers.

GDPR exports map submitted/charged jobs, uploaded files and print archives to their actual resource IDs. Order item SKUs and names are redacted in JSON and before invoice PDF rendering. Scheduling conflict errors use generic text for inaccessible jobs. Stored alert titles, messages and job names are redacted when read, using current membership.

The log viewer requires interactive superadmin access and explicit acknowledgement on a farm with controlled marks. Paged reads use `POST /api/admin/logs` with the `X-ODIN-Controlled-Ack: true` header after acknowledgement. Live streams first POST to `/api/admin/logs/stream-token` with that header, then GET `/api/admin/logs/stream?token=...`. Tokens expire after five minutes and are tied to the authenticated user; streams recheck policy, active account and admin authority before each line. This privileged access can disclose historical controlled names. A stream audits its opening and its termination when authority expires or is revoked, rather than every line. Refused log viewers are audited at most once per user per hour. Diagnostics and troubleshooting reports also use POST with the acknowledgement header; without acknowledgement they omit logs and explain why while returning the other results.

Deleting file-controlled jobs, including bulk deletion, organization deletion and automatic cleanup, saves audience tombstones within the deletion transaction without changing audit rows. Entitled members retain original details; other viewers receive redacted details, including searches and exports. Tombstone rules cover all media and audit rows referencing the deleted job ID, including terminal observations and alerts written after deletion. SQLite job creation uses a shared, writer-locked high-water allocator initialized in migration 055 from all retained job references; insert/delete triggers preserve the high-water mark across bulk deletion and restart. Migration 055 also guards the high-water row against deletion and decreasing updates. At boot, missing triggers are recreated and the high-water value is raised to cover all retained references. A missing singleton row with surviving identity triggers is refused: recover its all-time value from a trusted backup or exported job history using the INSERT command in the error before restarting. Rebuilding from current references alone could reuse an untraced ID. Repairs write a warning and a `job_identity.repaired` audit event. Altered trigger definitions are refused with an exact command to drop only the conflicting triggers; restarting ODIN then reinstalls the packaged guards. PostgreSQL keeps native sequence allocation. Public deletions create no tombstones or extra audit rows. Per-source clocks remain separate even for the same audience. Retention preview returns `tombstones` as `{job_id, audit_max_id}` objects; acknowledgement targets that pair and the caller's audiences. The retained `audit_max_id` identifies the deletion for acknowledgement; it no longer limits audit visibility protection. Audiences referenced by tombstones cannot be deleted (409 and an ON DELETE RESTRICT foreign key). Tombstones keep historical redaction active without triggering live-mark export acknowledgements. Migration 055 adds the high-water table, audience rules and persistent retention change histories. MQTT observes marks written by other processes within one second. Non-JSON, HEAD, redirect, empty-status and error responses bypass credential resolution in the response redactor. Component IDs are not model IDs; timelapse `print_job_id` identifies a scheduled job, unlike physical print events.

SQLite offline restore preserves the maximum of the live high-water value at apply time, the backup high-water value, and retained job references. This also applies to pre-055 backups, so IDs issued after a backup cannot be reissued after restoring it. A failure while carrying the ID floor rolls back the database swap. PostgreSQL offline restore captures the jobs sequence immediately before replacing data and keeps the larger live/restored sequence value after pg_restore, including during rollback; an unused sequence retains its initial allocation semantics.

The demo seeder uses `core.job_identity.insert_job`. Native parity probes intentionally retain raw job inserts for isolated PostgreSQL fixture graphs (native sequences or fixed relationship IDs), historical restore fixtures, or minimal pre-055 media schemas without an allocator table. Each probe documents its exception locally; `ops/database_parity/runtime_probe.py` is the PostgreSQL-only runtime/monitor fixture exception documented here. These probes are not SQLite production creation paths.
