# Offline license enforcement (slice A)

Applies to gaps 1, 3, 4 and 6 in the license-enforcement spec. This change does
not lock existing excess printers/users (slice B), revoke existing sessions, or
change the trial portal policy (gap 5). No schema migration is required.

## Clock and expiry

Signature and installation/device binding are checked before trusting signed
fields. A timestamp expiry is exclusive: `effective_now >= expiry` is expired
both during verification and in the cache. Historical date-only licenses keep
their end-of-UTC-day interpretation. Missing, blank or null `issued_at` remains supported for
older licenses; any other malformed value is refused. A wall clock more than
24 hours before signed `issued_at` or recorded time falls back to Community
with a clock-specific error. Skew of up to 24 hours is tolerated, but does not
extend the expiry deadline: the latest recorded time is still the lower bound
on effective time. Monotonic elapsed time advances that lower bound while the
process runs, even if the wall clock stops or moves backwards.

The high-water mark is a UTC timestamp in both:

- `/data/.odin-license-time.json` (uses `LICENSE_DIR` when configured), written
  with atomic replace, fsync and an interprocess lock;
- the existing `system_config` row `license_time_high_water` (JSON string).

Runtime entitlement reads checkpoint at most once per five minutes per process,
including the first read. Writers merge with existing evidence so an older
worker cannot lower a newer checkpoint. Candidate/preflight verification reads
time evidence but does not write checkpoints or create identity files. Runtime
time checks need neither network access nor a license portal.

An absent checkpoint is normal on a fresh install. Each storage copy is read
independently; unreadable/malformed copies are ignored. A working copy and the
process clock remain useful if one store fails. Write failures log a warning
and do not disable otherwise valid licenses. SQLite checkpoint writes do not
wait for a request's own write transaction; the file still records time, and
the database checkpoint is retried at the next interval.

Database backups already include `system_config`. Keep the file alongside the
database and device identity in volume backups. After restart, the maximum of
the file and database copies wins. A database-only rollback therefore retains
the volume's newer evidence; restoring the file alone retains database
evidence. A complete older snapshot can rewind both: that cannot be detected
offline. A snapshot with a later recorded time requires correcting the host
clock or the recovery procedure below.

### Recovery and limits

This deters casual clock rollback; it is not a secure time source. A determined
host owner can edit source or both stores, repeatedly restore complete
snapshots, or restart within the uncheckpointed interval. There is no hardware
fingerprint, online time request or new personal-data collection. Losing both
stores loses historical evidence. Up to five minutes of recent observation can
be lost on abrupt restart; sub-24-hour backwards changes across restarts can
also extend wall-clock use within the tolerated skew.

An accidentally advanced host clock can leave a future checkpoint. First fix
the host's UTC clock. If premium access still reports a clock error, log in
with the existing local admin password (and MFA if enabled). Password login,
license replacement, data access outside premium gates, and backups remain
available. No records are deleted by expiry. Keep a local admin credential
available before enabling SSO; an SSO-only account has no local password.

If the saved time itself was wrong, a host administrator can recover without
reinstalling or losing data: stop **all ODIN processes**, back up the database
and the clock file, delete **only** the `license_time_high_water` row from
`system_config` and `/data/.odin-license-time.json`, then restart with the
correct clock. Use the configured `LICENSE_DIR` if it differs. Do not remove
the license, installation ID or device key. Both copies and all process clocks
must be reset together; clearing just one intentionally does not reset trust.
This deliberate administrator action also defeats rollback protection, which
is an acknowledged limit of an offline, owner-controlled installation.

## Capacity and feature boundaries

Printer and user insertion services serialize count-plus-insert through
`system_config` rows `license_capacity_printers` and `license_capacity_users`.
Locks last until the caller commits or rolls back and work on SQLite and
PostgreSQL. Counts include inactive records, matching the existing creation
policy. API and setup printer creation share one check. User API, CSV import,
setup admin, OIDC provisioning, and both Classroom import paths share one
check. A failing Classroom batch rolls back its earlier inserts; CSV import
retains its existing per-row commit/partial-result behavior.

SSO authorization, callback and final token exchange require `sso`; local
password/MFA login does not. Public SSO configuration hides unavailable SSO.
CSV downloads require `csv_export`, including spool and print-log variants;
audit CSV keeps its additional Enterprise gate. Organization CRUD, membership,
printer assignment and settings require `user_groups`, matching `/groups`.
Existing organization data and tenant isolation remain intact on expiry.
Report schedule creation, edits, enabling and manual execution require email
notifications. Pausing with exactly `{"is_active": false}`, reading and deleting
retained schedules remain available. Delivery retains its existing email gate.

The calculator and pricing configuration are Community features inherited by
all tiers. Pricing changes retain their admin permission requirement.

The frontend refreshes license state at UTC expiry, on focus and after a
feature 403 from JSON/CSV or blob transport. An explicit response header
identifies feature denials, with a message fallback for older servers. Ordinary
RBAC denials do not trigger a refresh. Requests coalesce, timers handle long
licenses without overflow, and unmount removes timers/listeners. If browser
time is ahead of server time, an already elapsed deadline retries at most once
per minute; backend checks remain authoritative.
