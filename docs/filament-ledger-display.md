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

External spool IDs are returned as `external_spool_id`, never as Odin local spool
IDs. Slot edits, spool load/assignment/confirmation, QR assignment, manual slot
assignment, slot-count changes and filament sync are rejected with HTTP 409 for
linked printers. Unlinked printers keep their existing workflows. Printer
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
should not wait for hardware probes. Odin uses a two-second HTTP timeout and
15-second cache/backoff. Redirects and inherited HTTP proxies are disabled;
requests honor the existing ITAR destination policy. Missing printers, malformed
payloads and provider outages produce a visible warning while preserving the
printer list. The adapter does not expose provider error bodies.

## Verification

Backend checks use a temporary bootstrap database and a synthetic provider;
no running printer, inventory service, or login is required:

```sh
ODIN_TEST_BACKEND=backend python tests/printers/test_filament_ledger_display.py
```

Run the card regressions and production build from `frontend/`:

```sh
npm ci
npm test -- src/components/printers/LedgerDisplay.test.tsx
npm run build
```

The checks cover explicit bindings, tool numbering, original labels, unknown
amounts, cached reads, outages, organization scope, unchanged database rows,
blocked local edits and normal behavior for unlinked printers.
