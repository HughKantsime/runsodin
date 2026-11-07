# Audit integrity and SIEM streaming (Enterprise)

Enterprise adds `audit_integrity` and `siem_stream`: verification, signed exports,
and durable SIEM delivery. The audit chain and GDPR/retention behavior operate on
all tiers. Delivery and collection require a valid Enterprise entitlement. No
receiver is enabled by default, and no paid service or new account is required.

## Configure and operate

Open **Settings → Integrations → Audit integrity & SIEM** as a platform superadmin
(an admin without an organization). Configuration writes require an interactive
session. Read-scoped superadmin credentials can verify and export. Settings polling
is read-only; configuration changes are audited.

HTTPS receivers use `https://host/path`, an HMAC secret of at least 32 characters,
and optionally a PEM CA bundle for a private CA. Empty CA configuration uses system
trust. TLS certificate and hostname verification cannot be disabled. Secrets are
encrypted using the existing Fernet key and never returned by the settings API.
Leaving the secret blank preserves it; clearing the CA bundle restores system trust.
Query strings, embedded credentials, fragments, redirects, and environment proxies
are refused. The device destination policy permits LAN receivers, subject to ITAR
policy. Destinations are checked again for each delivery.

Syslog uses `syslog://host:port`, port 6514 with TLS or 514 without it. TLS uses the
default trust store unless a private CA bundle is supplied. A custom bundle replaces the default store, identically to HTTPS. Messages follow RFC 5424 (local0, informational), framed with
RFC 6587 octet counts. MSG contains the canonical audit JSON; MSGID is its audit ID.

Each destination has an independent worker, durable retry deadline and lease.
Failures back off the entire destination, starting at 10 seconds and increasing
to one hour, so fresh events cannot bypass its backoff. Workers reuse
HTTPS connections within batches of up to 50 events; syslog reconnects per event. Network I/O holds no database writer
transaction. A 60-second lease permits recovery after a worker crash.

The outbox is capped at 10,000 references across destinations. Appends drop the
oldest references first to make room, and append a chained `siem.gap` record with
the dropped count and ID range. Gap records do not recursively enqueue themselves.
Disabling a destination pauses collection/delivery and preserves its backlog.
License expiry stops collection and delivery. Both database providers compare
against the signed license expiry stored in audit_chain_state by the worker and
configuration transaction. Monitor processes read that same database value without
a process-local license cache. License removal/replacement is picked up within the
worker's 10-second refresh; expiry is checked on each append. When a destination is
enabled but collection is unavailable, the trigger writes a chained siem.gap with
the skipped audit ID and reason license_unavailable. Existing backlog remains capped.

Delivery is at least once. Receivers should deduplicate by installation and audit
ID (`X-ODIN-Event-ID` on HTTPS). A TCP write is syslog's available acknowledgement;
a crash after remote acceptance but before local acknowledgement can duplicate an
event. HTTPS requires 2xx. HMAC-SHA256 signs the exact body in
`X-ODIN-Signature: sha256=<hex>`. Last success, last error class, and pending count
appear in Settings. Error messages never retain credentials or payloads.

Changing a receiver endpoint with pending entries is refused. Deleting a receiver
discards its pending references and is audited. No historical backlog is sent to a
new receiver. The outbox stores IDs rather than copies of personal data; delivery
reads the current scrubbed row. Erasure cannot recall already delivered or in-flight
data. The receiver administrator controls receiver retention and deletion.

## Integrity, erasure, and retention

The migration identity is defined only in
[`audit_integrity_version.py`](../backend/core/schema/audit_integrity_version.py).
Runtime registration, validators, and restore guards use that identity.
The frozen through-046 restore bundle is historical and must not be renumbered.

Migration backfill reads canonical records in batches and calculates hashes in
Python, then performs one executemany UPDATE per batch. It remains atomic under
the migration lock and never repeats on startup. New rows use database INSERT
triggers covering ORM and raw writers. SQLite serializes writes; PostgreSQL locks
the singleton chain head. Actor stamping finalizes its own row before another
append. Rollback restores the row, head, and outbox together.

Canonical SHA-256 input starts with `odin-audit-v1:` and includes, in order:
`id`, `timestamp`, `actor_user_id`, `actor_username`, `actor_auth_kind`, `action`,
`entity_type`, `entity_id`, `details`, `ip_address`, `prev_hash`, `hash_salt`.
Every non-NULL value is its UTF-8 byte length, a colon, and its exact text; NULL is
`-1:`. Details are selected as `CAST(details AS TEXT)`, preserving stored JSON text.
Both providers normalize timestamps to `YYYY-MM-DD HH24:MI:SS.US`. Export and SIEM
use these exact fields, so non-erased row hashes can be recomputed from either.
Genesis is SHA-256 of `odin-audit-chain-v1:genesis`.

Each row has a separate random salt (SQLite randomblob; PostgreSQL two random
UUIDs). Erasure destroys the salt, retains the original row hash, and records a
signed transition without the original salt or plaintext. This prevents testing
guessed erased values against the original hash. Scrubbing allows actor/IP fields
to become NULL, the established anonymized-user detail form, and structural
`[deleted]`/`[deleted-user]` replacements. Arbitrary detail edits are refused.

Erasure validates only its target rows, batches receipts, and does not walk the
whole chain under the writer lock. An existing integrity break is recorded and
erasure proceeds; a receipt marked invalid cannot make the verifier accept the
corrupted row. `audit_redactions` provides indexed verification lookup. The same
receipts appear in an `audit.erasure` audit record for export. Each receipt binds
the original hash, final content hash, immutable fields, target ID, erasure record
ID, and operation identifier. HMAC-SHA256 authenticates receipts using a
dedicated installation audit HMAC keyring stored outside the database. Each receipt
and checkpoint names its key ID. Preserve `.audit-hmac-keys.json` across restore;
JWT rotation does not affect verification. Retired audit keys remain in the keyring.
See `SECRET_ROTATION.md` for rotation and recovery.

`audit_logs_days` works on every tier. With licensed audit integrity it must be
at least 90 days, or 0 to retain forever. Existing shorter policies are clamped to
90 at read and cleanup time, including after a license upgrade. Cleanup deletes expired events and pending
references, retaining authenticated checkpoints for each deleted sequence range.
Each checkpoint records the preceding hash and deleted range's last hash.
Corruption in a deleted row is recorded and remains a verification failure. It also
appends one `audit.retention` summary per run (deleted and range counts, including zero), linked to checkpoints by operation ID; remaining rows keep their hashes. Erasure/retention
accountability records remain as evidence. No history is silently rehashed.
Education's separate immutable audit history remains protected by its own policy.

This is tamper evidence, not protection against an administrator controlling the
application secrets and all database state. Preserve independent signed exports
or SIEM copies for stronger evidence.

## Verification and signed exports

`GET /api/audit-logs/verify` returns `ok`, the first break and reason, and every
break with a stable fingerprint and whether it is already known. NULL sequences,
row-count mismatches and the independently saved last signed head are checked.
A signed invalid-before receipt/checkpoint preserves a known failure without
preventing inspection of later rows. Missing or unauthentic saved status after
the first run reads attention. Verification
and export use a separate read-only snapshot: SQLite WAL read transaction or
PostgreSQL REPEATABLE READ READ ONLY. The head and rows come from the same snapshot;
no writer/chain lock is held during the walk.

Daily verification authenticates its saved cursor and checks due-ness before writing. It resumes from the previous
verified head, does a full walk at least weekly, and does a full walk after erasure
or retention. A newly observed break raises a critical `audit_integrity` alert for
platform superadmins. Public `/health` exposes only `ok` or `attention`, without
row IDs, break details, or license state. Investigate failures; do not rehash them.

The date-bounded JSON Lines export has no 5,000-row cap:
`GET /api/export/audit-logs/jsonl?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD`.
Both inclusive UTC dates are required. Output is spooled to a private temporary
file and streamed in 64 KiB chunks, without unbounded application memory.
The CSV endpoint retains its existing behavior and cap.

The existing installation Ed25519 device key signs the exact compact JSON
manifest. Export refuses a missing identity and never creates one. The manifest
contains the JSONL SHA-256, count, date bounds, chain head/count, genesis,
installation ID, SHA-256 public-key fingerprint, and `known_breaks`. Unknown breaks
refuse export until daily verification records them; signed erasure/retention
breaks are already recorded. Exports include only positive chained sequences. Headers
`X-ODIN-Audit-Manifest`, `X-ODIN-Audit-Signature`, and `X-ODIN-Audit-Public-Key` are
base64; `X-ODIN-Audit-Chain-Head` is hex. `bundle=true` packages JSONL, manifest,
signature, and public key as a ZIP (the Settings download).

```python
import base64, hashlib, json, zipfile
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
with zipfile.ZipFile('audit-logs.zip') as z:
    manifest = z.read('manifest.json')
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(z.read('public-key.ed25519.b64')))
    key.verify(base64.b64decode(z.read('signature.ed25519.b64')), manifest)
    digest = hashlib.sha256()
    with z.open('audit-logs.jsonl') as rows:
        while chunk := rows.read(65536):
            digest.update(chunk)
    assert digest.hexdigest() == json.loads(manifest)['sha256']
```

Compare the public key against an independently trusted installation key. A date
slice can exclude receipt/checkpoint records; use a full-range export for available
chain evidence. Destroyed salts mean erased hashes require receipt evidence,
rather than direct recomputation. Receipt HMAC verification requires the retained
audit HMAC keyring; the device-signed export attests to the exported evidence.

## Restore compatibility

New tables are `audit_chain_state`, `audit_siem_destinations`, `audit_siem_outbox`,
`audit_integrity_status`, `audit_redactions`, and `audit_retention_checkpoints`.
All are in the backup inventory. PostgreSQL executable guards and SQLite restore
admission use the source-owned append trigger. SQLite backups before this migration
remain valid: actor columns alone do not require the new append trigger.

Existing release ledgers and frozen sources are preserved.
Receiver endpoints, HMAC secrets, and optional private CAs are local customer
configuration.

## Retention processing, shared audit files, and SIEM delivery

Retention walks indexed sequence ranges in 256-row pages; erasure spools its
preimages to an unlinked private temporary file and records bounded receipt
batches within the original transaction. Neither constructs a user-sized IN list.
SQLite explicitly chains internally created `siem.gap` events, without recursive
triggers. SQLite and PostgreSQL both read the shared `siem_until` deadline, refreshed by maintenance and destination configuration. Triggers never read identity/license files.

Daily checks share `.audit-daily.lock` across workers on the installation volume.
The independent `.audit-signed-head.json` anchors full walks, including when
retention carries that head into a signed checkpoint. Keep the audit key directory
on a persistent shared volume for all API workers. It defaults to `LICENSE_DIR`
(`/data`), or `ODIN_AUDIT_KEY_DIR` when explicitly configured.

Private CA bundles apply to HTTPS and syslog TLS. Syslog reconnects for each
one-event batch and performs bounded read-ahead before deleting its delivery
reference, retrying observed FIN/RST/TLS close. RFC5424/TCP provides no receiver
application acknowledgement: success means the socket accepted the bytes without
an observed close, not proof of durable receiver storage. Use HTTPS with a
receiver that acknowledges persistence when that guarantee is required.

## Restore continuity and missing audit keys

Daily verification keeps advancing the signed head after a break. Each head
records after_break (the number of distinct breaks observed), unresolved continuity
breaks, and an authenticated history of earlier heads. A later tail deletion is
therefore checked against the latest head even after a restored backup broke an
older anchor. Unresolved continuity failures remain visible until reviewed.

Restore flows already append restore_staged/restore_completed events. Restore does
not silently accept a baseline. After investigating, an interactive platform
superadmin supplies a reason in Settings or POST /api/audit-logs/accept-restore-baseline.
The audit.restore_baseline_accepted event records actor, reason, head and prior
breaks; the head history remains. Acceptance cannot hide row/checkpoint corruption.

If sealed evidence exists but .audit-hmac-keys.json is missing, verification and
health return audit_key_missing, with no fabricated tampering breaks. Daily checks,
erasure and retention cannot mint replacement keys; signed export refuses with 409.
Recover the original keyring. See BACKUP_RESTORE_RUNBOOK.md for both audit files
and the native-backup companion/bundle. Rotation must run as the directory owner:
docker exec -u odin -w /app/backend odin python -c 'from core.audit_keys import rotate; rotate()'.


### Explicit keyring initialization

Only evidence with both a `key_id` and `signature` blocks automatic first-use key
creation; an unsigned erasure, retention or baseline action cannot establish that
keys were lost. This presence check cannot authenticate an envelope without its
keys. The verifier still checks signatures whenever keys are available.

When original keys cannot be recovered, an interactive platform superadmin with
Enterprise can use Settings → Initialize audit keyring or
`POST /api/audit-logs/initialize-keyring` with a reason of at least ten characters.
The action only runs in `audit_key_missing`. A chained `audit.keyring_initialized`
record (retention-exempt) and critical alerts record actor, reason and new key ID
before the replacement keyring is published. No key material is logged. Existing
keys are never overwritten, and earlier unsealed evidence remains unverified.
Old signed receipts, checkpoints, status and heads are preserved, not re-signed.

### Recovery and evidence rules

Required GDPR erasure and retention proceed even when the audit key directory is
lost. Receipts/checkpoints explicitly carry `unsealed: true` and
`reason: audit_key_missing`; they are never presented as authenticated evidence.
Verification reports missing keys as installation state, and reports unsealed
transitions as `unverified_deletion` or `unverified_redaction`: they "can't be told
apart from tampering". Once a keyring exists these remain suspect deletion/content
breaks, never a claim that keys are still missing. Signed exports and restore-baseline acceptance
continue to refuse unknown breaks.

Baseline acceptance consumes the latest native `restore_completed` marker and
requires an authentic audit HMAC signature and a restore time newer than the
authenticated pre-restore anchor. Both native writers seal completion details;
unsigned or modified markers cannot grant acceptance.
Daily checks carry pending restore authority forward without accepting it.
Acceptance and restore completion audit rows are excluded from retention.

While SIEM entitlement is unavailable and a destination is enabled, the append
trigger opens one immutable chained `siem.gap` record. `audit_siem_gaps` extends
its skipped count and last event ID under the same writer lock. These counters
are operational observations, not signed audit payload; the verification API
returns the latest 100 as `siem_gap_observations`. A licensed append or an append
with no enabled destination closes the stretch and appends `siem.gap_closed`,
carrying the gap ID, first/last IDs and final skipped count. This record is chained
and queued for enabled SIEM destinations when licensed; if all destinations are
disabled it remains in the audit log. A later lapse opens a new stretch.
Retention keeps an open gap; once closed, it removes the counters with the opening row. Both providers back up
this table with the database.

External head history keeps the latest 16 transitions, daily heads for 90 days,
then weekly heads, with a hard cap of 210 routine entries. Acceptance entries
are retained permanently, independently of buckets and the routine cap; installations
with many accepted restores can exceed 210 total entries. Each entry is independently
sealed. A signed current head binds a digest of the compacted history; saving
signs only the new compact receipt and current head, not the entire history.

## Controlled audit exports and SIEM trust

Signed JSONL exports and SIEM delivery retain canonical audit rows, including controlled
names and reasons; redaction would invalidate per-row hash verification. SIEM receivers
are part of the customer's trusted boundary, unlike generic webhooks and MQTT, which
continue to use generic controlled-work copy.

An interactive superadmin acknowledges each saved destination with
`POST /api/settings/siem/{id}/acknowledge` and `X-ODIN-Controlled-Ack: true`
under the same-origin check. The standing acknowledgement records who and when in
`system_config` (no migration), binds to the receiver and transport settings, and
writes `controlled_part.siem_acknowledged`. It can be granted before or after marks
exist. API tokens cannot acknowledge destinations. Settings shows its author and time.

When live marks or retained read restrictions exist, save a new destination disabled,
acknowledge it, then enable it. Enabling requires a valid standing acknowledgement.
Changing the endpoint, transport, TLS, signing secret or CA clears the acknowledgement;
deleting a destination removes it. Existing pending deliveries must drain before an
endpoint/transport/TLS change. Marking requires every enabled destination to have a
standing acknowledgement; otherwise `controlled_part_siem_acknowledgement_required`
names the destination a superadmin must acknowledge. An entitled operator can then
create the first mark without a superadmin joining the audience. Mark requests have
no SIEM acknowledgement flag.

Signed JSONL GET exports require acknowledgement when live marks or retained read restrictions exist. The UI retries only
after confirmation through `POST /api/export/audit-logs/jsonl/acknowledge`, preserving
the date and bundle parameters, with an interactive superadmin and
`X-ODIN-Controlled-Ack: true` under the same-origin check. The download records a
`controlled_part.privileged_export` event with kind `audit_jsonl`. Protect downloaded
copies and SIEM storage according to the controlled audience's handling requirements.

Controlled policy reasons use `[erased]` during GDPR erasure. Audience save/delete
audit snapshots remove the erased member ID from before/after lists through authenticated
E1 receipts; other members and policy fields remain unchanged.
