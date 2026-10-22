# Square finished-stock connection

The Square panel inside **Products** reads stock and manages local connection/mapping evidence. Explicit inspected additions have a separate, opt-in send operation. Square remains the finished-inventory authority. Mapping a product, reading stock or completing an ODIN job does not increment Square inventory or publish a listing. There is no label, payment, catalog mutation or physical-count write operation.

An administrator signed in through an interactive session can configure the connection. ODIN API tokens cannot create, replace or remove it. Each organization has its own connection; a platform administrator's unassigned products use a separate connection. Shared or foreign products cannot borrow an organization's Square mapping.

## Setup

1. Ensure the existing ODIN server encryption key is available and preserved in backups. Do not regenerate it to configure Square.
2. Open Products, then Square inventory. Select Sandbox or Production deliberately.
3. Enter an existing Square developer access token in the password field. Required API access includes merchant profile, catalog items and inventory reads. Do not paste tokens into chat, tickets or logs. No new paid service is required by this connector.
4. Verify and connect. This checks merchant identity. Catalog and inventory permissions are exercised when those reads are requested; identity verification alone does not prove them.
5. Choose Square item, then explicitly select an existing ODIN product, active Square location and Square variation. No automatic SKU matching occurs. The selected variation and its parent item must be available at that location, with inventory tracking enabled there.
6. Read current stock. The display retains Square's decimal quantity, read time and calculation time. Missing counts remain unknown. Failed refresh clears the old displayed count rather than presenting it as current.

The token is encrypted on this ODIN server, never returned by these APIs and excluded from the support bundle. Configuration is covered by existing encrypted-server/database backup practices; the correct encryption key is required after restore. Changing merchant or environment removes local mappings, baselines and the tenant write flag. Replacing a token for the same account/environment keeps mappings, baselines and the tenant write flag. Configuration edits invalidate a preflight in progress; unrelated edits between attempts do not invalidate the frozen destination. Disconnecting removes local configuration, mappings, baselines and activation. Receipts remain retained.

Catalog browsing is explicitly paginated. Counts are on demand, without background polling, personal reminders, webhooks or an additional maintained stock count. A second product cannot map to the same variation/location within the connection. Deleted products are pruned from the mapping; organization deletion removes its connection.

## Activation and explicit stock sends

Writes require **both** keys. `ODIN_SQUARE_STOCK_WRITES` is read once when the server process starts; only the exact string `1` enables it. Missing, empty, `0`, `true` and every other value are off. Restart the container after changing this switch. The encrypted tenant connection stores a strict boolean `stock_writes_enabled`, absent means false. An interactive admin sets it with `PUT /api/products/square/activation`, body `{"revision":"<current CAS revision>","stock_writes_enabled":true}`. This is audited; stale revisions return 409. Same-account/environment credential rotation preserves it; changing account/environment or disconnecting clears it.

The Products history Send button is visible only for prepared additions while both keys are on. Checking reads the merchant, active locations, tracking and token scopes, then displays product, quantity, location and current Square count for confirmation. An interactive operator/admin confirms one addition per `POST /api/products/batches/additions/{id}/send`. Viewers, API tokens and noninteractive credentials cannot send. Acceptance never sends automatically; there is no worker, batch send or retry endpoint.

The executor performs fresh scope/identity reads in that same request, then rechecks configuration revision, both activation keys, baseline versions and reviews under the connector-row CAS immediately before `prepared/0 → unknown/1`. This also fences global owner 0. Disable prevents later reservations; an already-reserved send may complete. The saved bytes and idempotency key are used once. A 401/403, timeout, 5xx or malformed response leaves an unknown outcome, never permission to resend. Reconcile only through exact history. Read-only replacement credentials can reconcile without `INVENTORY_WRITE`.

The read-only scope preflight is `POST /oauth2/token/status` on the fixed environment host; it requires a well-formed `scopes` list containing `INVENTORY_WRITE`. Failed introspection blocks before reservation. The production endpoint was confirmed in design review; live production and sandbox behavior remain rollout verification, not inferred from mocked tests.

## Baseline cutover

First set tracking and the physical baseline in Square Dashboard with owner authorization. ODIN never writes a physical count. An interactive admin then attests the exact mapped destination via `PUT /api/products/{product_id}/square/baseline`, body `{"revision":"<current CAS revision>","at":"2026-10-22T12:00:00Z","quantity":300}`. ODIN records the admin and a fresh random UUID version. Replacing it always mints a new UUID. The response includes `pending_additions` requiring review. Unknown additions posting that product cause 409 until reconciled; the unknown query and baseline persistence hold the same connector fence as reservation.

A baseline belongs to the exact account/environment/location/variation. Destination changes or removing the mapping clear it; saving the same destination preserves it. See [production inspection](PRODUCTION_BATCH_INSPECTION.md) for per-output choices and the addition review barrier. Sales sync, replenishment, assemblies and automatic posting remain out of scope.

## API and verification

APIs live under `/api/products/square` (connection status/configuration and locations/catalog reads) and `/api/products/{product_id}/square` (mapping and stock). Each preflight/configuration transaction uses a current revision CAS. Pending additions freeze destination identity, not a permanent connector revision. Local mapping/configuration writes do not invoke Square writes.

Square API version: `2026-09-16`. Only the following Square operations are allowed: merchant read, location read, catalog list/retrieve inventory count retrieval, token-status introspection and inventory-history reconciliation. Count retrieval and token status use HTTP POST as reads. The separate gated mutation transport permits only batch inventory adjustments from saved receipts.

Official references: [Merchant identity](https://developer.squareup.com/reference/square/merchants-api/retrieve-merchant), [Catalog](https://developer.squareup.com/reference/square/catalog-api), [Inventory counts](https://developer.squareup.com/reference/square/inventory-api/batch-retrieve-inventory-counts).

Implementation state and exact evidence: `.claude/specs/sublab-square-production.md`. A successful synthetic test is not confirmation that the owner's real Square account or stock baseline is connected.
