# Square finished-stock connection

This first workflow slice adds a read-only Square panel inside **Products**. Square remains the finished-inventory authority. Mapping a product, reading stock or completing an ODIN job does not increment Square inventory or publish a listing. There is no label, payment or stock-adjustment operation in this connector.

An administrator signed in through an interactive session can configure the connection. ODIN API tokens cannot create, replace or remove it. Each organization has its own connection; a platform administrator's unassigned products use a separate connection. Shared or foreign products cannot borrow an organization's Square mapping.

## Setup

1. Ensure the existing ODIN server encryption key is available and preserved in backups. Do not regenerate it to configure Square.
2. Open Products, then Square inventory. Select Sandbox or Production deliberately.
3. Enter an existing Square developer access token in the password field. Required API access includes merchant profile, catalog items and inventory reads. Do not paste tokens into chat, tickets or logs. No new paid service is required by this connector.
4. Verify and connect. This checks merchant identity. Catalog and inventory permissions are exercised when those reads are requested; identity verification alone does not prove them.
5. Choose Square item, then explicitly select an existing ODIN product, active Square location and Square variation. No automatic SKU matching occurs. The selected variation and its parent item must be available at that location, with inventory tracking enabled there.
6. Read current stock. The display retains Square's decimal quantity, read time and calculation time. Missing counts remain unknown. Failed refresh clears the old displayed count rather than presenting it as current.

The token is encrypted on this ODIN server, never returned by these APIs and excluded from the support bundle. Configuration is covered by existing encrypted-server/database backup practices; the correct encryption key is required after restore. Changing merchant or environment removes the local mappings. Replacing a token for the same account/environment keeps them but invalidates in-flight requests. Disconnecting removes only local configuration and mappings.

Catalog browsing is explicitly paginated. Counts are on demand, without background polling, personal reminders, webhooks or an additional maintained stock count. A second product cannot map to the same variation/location within the connection. Deleted products are pruned from the mapping; organization deletion removes its connection.

## Not yet delivered

This slice does not implement sales-event ingestion, replenishment rules, approved printer-specific recipes, inspected accepted quantities, Square stock adjustments, assembly, conversational starts or R1 control. The next slice must distinguish physical completion from sellable acceptance and use durable idempotent stock operations. It must not derive finished stock from existing order fulfillment alone.

## API and verification

APIs live under `/api/products/square` (connection status/configuration and locations/catalog reads) and `/api/products/{product_id}/square` (mapping and stock). Every connected operation is bound to a current connection revision. Local mapping/configuration writes do not invoke Square writes.

Square API version: `2026-09-16`. Only the following Square operations are allowed: merchant read, location read, catalog list/retrieve and inventory count retrieval. The count-retrieval endpoint uses HTTP POST as a read; inventory-change endpoints are not available.

Official references: [Merchant identity](https://developer.squareup.com/reference/square/merchants-api/retrieve-merchant), [Catalog](https://developer.squareup.com/reference/square/catalog-api), [Inventory counts](https://developer.squareup.com/reference/square/inventory-api/batch-retrieve-inventory-counts).

Implementation state and exact evidence: `.claude/specs/sublab-square-production.md`. A successful synthetic test is not confirmation that the owner's real Square account or stock baseline is connected.
