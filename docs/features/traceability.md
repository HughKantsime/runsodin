# Part traceability and certificates of conformance

Enterprise's `traceability` entitlement adds a panel on **Jobs → Part traceability & certificates**. Enter a completed job ID to view its record, record a pass/fail inspection with notes, mark the attempt delivered, or download a signed certificate bundle. Administrators configure inspection policies in the same panel. A date range exports a ZIP containing each job's PDF, signature and JSON record.

## What the record means

A record is captured in the job-completion transaction, alongside lifecycle evidence. Capture runs inside a savepoint; a capture error is logged without rolling back completion, and a later read can capture it with the late-capture label. Legacy completed jobs without a record are captured at first access and carry `captured_after_completion` in JSON and on the PDF. It is keyed by the existing physical print-attempt counter. Resetting a job starts a separate record and does not carry an inspection or delivery forward. Existing records remain readable by `(job, attempt)` after resets, printer deletion or reclassification; add `?attempt=0` to the JSON, PDF or signature URL. Live binding drift is a warning on both reads and delivery; inspection results and policy determine whether delivery is allowed.

The retained snapshot includes the exact job-bound file name and retained SHA-256, model revision, printer name/model/serial, nozzle information, recorded spool usage and material/colour/lot/vendor, retained slicer settings, start/end timestamps and outcome. Missing facts remain null. File bytes are never included or read from the filesystem during export. A missing exact file binding is not reconstructed from the current model's file.

**Historical limits are explicit:** missing print-time facts cannot be reconstructed. Legacy captures are labelled as late. Printer identity/configuration and spool descriptions are read at capture time. Nozzle diameter comes from the retained slicer file; installed nozzle observations are retained separately. Physical print spool snapshots preserve the attempt's AMS/tray evidence. The file hash prefers the dispatched sender digest linked through its dispatch receipt, then the job digest, then the file digest; `hash_source` identifies it. Serial is available from the existing encrypted Bambu configuration; other protocols do not persist a serial field. Filament attribution comes from recorded usage for the attempt, never from whichever spool is currently loaded. Archive accounting can arrive after completion: rendering adds append-only usage with an explicit physical-attempt binding, even after a later reset. The stored snapshot and its digest remain unchanged; late usage is labelled `recorded_after_capture`. Late spool descriptions prefer the captured start evidence, with current metadata only as an explicitly documented fallback. Legacy physical usages without an attempt binding are omitted rather than assigned to a different print. Keep independent incoming-material and calibration records where required.

Operator IDs are frozen from this attempt's lifecycle actors and submitter/queued-by evidence; names resolve at render time. QA and export readers do not become operators. JSON and PDFs expose only the curated lifecycle list, without raw audit usernames or details. Ready timelapses are linked only when bound to the same print attempt; the existing video endpoint performs its own authorization. Timelapse deletion leaves no permanent media copy in a certificate cache.

## Inspection and delivery

A policy targets an organization's exact, case-sensitive **printer tag** (the existing fleet grouping mechanism) or **product ID**. Product and tag matching use the scope frozen in the snapshot, even if the current printer or order item changes. Either matching required policy requires a passing inspection. An explicit failed inspection blocks delivery even when inspection is optional. Turning off one policy does not override another required policy.

The existing job lifecycle has no delivered state. Standalone jobs retain a separate delivery receipt on the completed attempt. All three order shipping paths—status PATCH, `/ship`, and show-order handoff—use the same inspection gate under the order lock and record delivery in their shipping transaction. The gate covers both direct order-item linkage and the immutable production-job registry. When traceability is not licensed, shipping skips this gate entirely. Delivery is idempotent, and the job/group rows serialize delivery with inspection, reset and policy changes. Once delivered, its inspection is locked. Policy changes do not retroactively undo recorded deliveries.

Policies use the organization ID, or `0` for platform-owned jobs. Only a superadmin can read or configure platform policies. Products used outside an order item are not inferred from matching models.

## Authorization and API

Both `/api` and `/api/v1` expose:

| Method | Path (relative to prefix) | Permission |
| --- | --- | --- |
| GET | `/jobs/{job_id}/traceability` | `jobs.traceability` |
| GET | `/jobs/{job_id}/certificate` or `/jobs/{job_id}/certificate.sig` | `jobs.traceability` |
| GET | `/jobs/traceability/export?start=…&end=…` | `jobs.traceability` |
| PUT | `/jobs/{job_id}/inspection` | `jobs.inspect` |
| POST | `/jobs/{job_id}/deliver` | `jobs.deliver` |
| GET, PUT | `/jobs/traceability/policies` | `jobs.inspection_policy` |

All routes require `traceability` through the existing feature refusal. Record/export/inspection/delivery permissions default to admin/operator; policy management defaults to admin. They are editable through Settings → Permissions. API tokens additionally need `read:jobs` or `write:jobs` as appropriate. Tenant callers cannot export another tenant's or platform-owned jobs. Existing Education-owned jobs retain the generic-workflow refusal: this feature does not grant classroom authority or alter protected Education history.

Inspection body: `{"result":"pass","notes":"Dimensions checked"}` (`fail` is also allowed; notes maximum 4,000 characters). Policy body: `{"org_id":1,"target_kind":"printer_tag","target_key":"QA","required":true}`; use `"product"` and a decimal product ID for product policies. GET policies requires `org_id`.

Batch ranges require timezone offsets, use completion time, and are start-inclusive/end-exclusive. The UI converts local midnight boundaries independently, including daylight-saving changes. Maximum range is 366 days. More than 500 matching jobs is explicitly refused with HTTP 413; select smaller ranges. Education jobs are excluded. Jobs refused during the per-job authorization check are skipped with a reason in the manifest. A 100 MiB cap applies to both total uncompressed content and the ZIP. Each job uses a short read/capture transaction; PDF rendering happens outside that transaction and the ZIP streams from a spooled file. No matching jobs produces an empty manifest. There is no silent truncation.

Reads, exports, inspection changes, delivery, policies and inspection-related delivery refusals write audit rows. Successful business mutations and their audit events commit together. Export generation/signing/audit failure sends no successful artifact. Per-job captures committed before rendering remain retained manufacturing evidence.

## Verify a certificate

Use `GET /jobs/{job_id}/certificate?bundle=true` for a ZIP containing `certificate.pdf`, `signature.json` and `traceability.json`. Without `bundle`, the response is a PDF with `X-ODIN-Signature`, `X-ODIN-Public-Key` and `X-ODIN-SHA256` headers. The `.sig` endpoint returns the detached JSON proof for the same current PDF. Certificates use the configured company name, accent colour and an eligible local uploaded logo; no remote assets are fetched.

`signature.json` contains the Ed25519 signature and raw public key as base64, plus the PDF's SHA-256. The signed message is `b"odin-traceability-certificate-v1\n" + SHA256(pdf).digest()`. Export only reads the existing installation key and returns a clear 503 if it is unavailable; it never provisions one. The key fingerprint and snapshot digest are printed on the PDF. Unsupported glyphs render as `[?]`; JSON remains authoritative. Verify without re-saving the PDF:

```python
import base64, hashlib, json
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
proof = json.loads(Path('signature.json').read_text())
key = Ed25519PublicKey.from_public_bytes(base64.b64decode(proof['public_key']))
digest = hashlib.sha256(Path('certificate.pdf').read_bytes()).digest()
key.verify(base64.b64decode(proof['signature']), b'odin-traceability-certificate-v1\n' + digest)
```

Compare the public key against a separately trusted installation key. A public key bundled with a file alone does not establish who owns that key. This is a detached signature, not an Adobe/PAdES signature. The signature covers the PDF (including its evidence appendix); the companion JSON is provided for convenience and is not independently signed. It does not certify a regulatory standard or replace the operator's inspection. Failed/uninspected records explicitly say conformance is not established.

## Privacy, retention and migration

Migration `python:050-traceability` adds `traceability_records` and `traceability_policies`, registers both in the post-027 ledger and backup inventory, and changes no existing rows or ORM baseline. It adds no PostgreSQL functions or triggers. SQLite and PostgreSQL use the same DDL and exact-shape validator. The PostgreSQL round-trip proof includes both new tables. A new `preserved046` bundle retains the released pre-E4 restore path; older frozen bundles remain unchanged. Native PostgreSQL execution requires the review environment.

The license facade retains `license_manager.TIERS` and existing import/API behavior. To keep source files under 700 lines, device-key persistence, license information, tier definitions and trial-message encoding use the same module split as the parallel E1 build; only the Enterprise `traceability` flag is added here.

GDPR export includes records associated with the user's submitted/charged jobs and their inspection/delivery actions. Erasure clears inspector/deliverer references, scrubs known identifiers from inspection notes while preserving quality findings, removes frozen operator IDs, and scrubs technical snapshot text. Result, timestamps, attempt linkage and de-identified manufacturing facts remain. Ordinary hard user deletion invokes the same cleanup. Deletion of a job with any delivered record is refused, including bulk deletion after reset and organization cleanup. Retention skips delivered jobs while still deleting other eligible jobs. The database foreign key uses ON DELETE RESTRICT; authorized deletion explicitly removes only undelivered records first; organization deletion cascades its policies. Personal exports omit notes written by another inspector. Snapshot erasure recomputes `snapshot_sha256` and writes an audit event. No scheduled retention rule or second permanent PDF store is added. Downloaded copies and external backups are not rewritten; future certificates are generated from the current scrubbed record and curated lifecycle.

No credentials or paid services are needed. Claude review and native PostgreSQL restore proof remain release review gates. The full census is deliberately excluded from this shared-Mac review run. E3 controlled-part authorization remains a merge note and is not implemented here. The public pricing page belongs to the separate `odin-site` repository and is outside this checkout; lead Enterprise wording with “Part traceability and signed certificates of conformance,” alongside the other tier-value work.
