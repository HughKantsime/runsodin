# Read-only external filament display

Odin can display tool-to-spool mappings maintained by an external service using
the Filament Ledger display-feed contract. This is useful when an existing
inventory or accounting service owns the assignments. It does not transfer
assignment or consumption ownership to Odin.

The integration is disabled by default. No database migration is required.
Configure the following environment variables on the Odin server:

```yaml
environment:
  FILAMENT_LEDGER_URL: http://filament-ledger:8080
  FILAMENT_LEDGER_PRINTERS: '{"1":"example-printer"}'
```

An omitted or blank `FILAMENT_LEDGER_PRINTERS` disables the integration.
Malformed nonblank JSON is rejected at startup.

The JSON object maps an Odin printer ID to an external printer key. Bind printers
explicitly; Odin does not guess from names or change physical tool assignments.
The source must be reachable from the Odin container. Deploy it on a trusted
private network. No browser-to-provider connection or new public route is needed.
Authenticated printer reads retain their role and organization checks.

Linked printer cards show tool labels, mapped spool names, original materials,
colors when known, and remaining percentages only when both weights are known
and initial weight is positive. The card identifies the source as read only.
A mapping is an inventory assignment, not proof that the physical filament is
loaded. Manage assignments in the external service's own interface.

`loaded_colors` is empty for linked printer responses, including during outages,
so stale local slot labels are not presented alongside the external display.

External spool IDs are returned as `external_spool_id`, never as Odin local spool
IDs. Slot edits, spool load/unload/assignment/confirmation, QR assignment, manual slot
assignment, slot-count changes and filament sync are rejected with HTTP 409 for
linked printers. This includes AMS sync and every hardware slot configuration
route (Odin's AMS sync and Bambu sync/configuration endpoints); they all return
HTTP 409 without contacting the printer. Moving a local spool out of a linked
printer is also rejected. Unlinked printers keep their existing workflows. Printer
controls and Odin's other integrations remain independent; this display adapter
never writes assignments, weights, or printer settings. If an external service
owns consumption, configure Odin's other consumption integrations accordingly.

## Provider contract

Odin calls only `GET <FILAMENT_LEDGER_URL>/api/filament-display`. A compatible
provider returns this minimal JSON structure:

```json
{
  "printers": [{
    "key": "example-printer",
    "tools": [{
      "tool_index": 0,
      "display_name": "Tool 1",
      "spool_id": 42,
      "spool_status": "mapped",
      "spool": {
        "id": 42,
        "material": "PLA+",
        "name": "Blue matte",
        "brand": "Example brand",
        "color_hex": "3366AA",
        "remaining_weight_g": 750,
        "initial_weight_g": 1000
      }
    }]
  }]
}
```

Tool indexes are zero based and unique within each printer. Odin presents index
0 as slot 1. Spool identities must agree between `spool_id` and `spool.id`.
Material, name, brand, color and weights may be null when unknown. Colors use
six RGB hex digits, optionally prefixed with `#`; weights must be finite numbers.
Original material labels are preserved in `material_type` even when they do not
match Odin's local material enum.

For an unmapped tool, set `spool_id: null`, `spool_status: "unmapped"`, and
`spool: null`. If a mapping exists but its inventory details cannot be read,
retain `spool_id`, set `spool_status: "unavailable"`, and `spool: null`.
Odin distinguishes these states instead of claiming an empty or full spool.

Serve a cached display snapshot with bounded inventory reads. A display request
should not wait for hardware probes. Odin applies one two-second deadline to the
whole refresh, including DNS resolution, connect, headers and the response body,
and caps the response at 512 KiB. A read that finds the cache expired starts one
background refresh; printer reads always serve the last completed snapshot (or
`unavailable` when none exists) instead of waiting for it. The 15-second
cache/backoff starts when the refresh finishes, not when it began. Redirects
and inherited HTTP proxies are disabled; requests honor the current device
destination policy and ITAR restriction.
The connection is DNS-pinned even outside ITAR mode. Loopback, link-local,
metadata and ODIN service endpoints are blocked; use a private service hostname
or LAN address. Missing printers, malformed
payloads, oversized responses, timeouts and provider outages produce a visible
warning while preserving the printer list. The adapter does not expose provider
error bodies.

## Verification

Backend checks use the shared contract database fixture and a synthetic provider;
no running printer, inventory service, network egress or login is required:

```sh
python -m pytest tests/test_contracts/test_filament_ledger_display.py \
  --confcutdir=tests/test_contracts
```

Run the card regressions and production build from `frontend/`:

```sh
npm ci
npm test -- src/components/printers/LedgerDisplay.test.tsx
npm run build
```

The checks cover explicit bindings, tool numbering, original labels, unknown
amounts, cached reads, outages, the whole-refresh deadline, response-size cap,
single-refresh concurrency, expiry measured from completion, organization scope,
unchanged database rows, blocked local edits and cross-printer moves, the
inventory/printers contract boundary, destination-policy rejection,
organization-safe errors and normal behavior for unlinked printers.

The display response is never written back to the ORM. ODIN's stored slot
inventory, telemetry observations, consumption accounting and dispatch decisions
remain independent of this feed. In 2.x, organization and shared-printer visibility
are applied before enrichment; external IDs are exposed only through those
authorized printer responses.
