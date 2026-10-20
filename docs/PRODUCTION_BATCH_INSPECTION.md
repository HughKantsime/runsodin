# Recipes and inspected production batches

This local development slice adds a closed-by-default panel under **Products**. Square remains the finished-stock authority. The application can prepare additions but **cannot send them**: no delivery endpoint or automatic worker is installed. A fixed-host Inventory transport is implemented behind a hard disabled code gate; no setting or UI can activate it.

## Workflow

1. Complete a single-attempt ODIN print with retained dispatch and physical observation evidence.
2. Open **Recipes and finished batches**. An administrator creates a recipe revision using that successful reference job, material/color requirements and fixed main/flush yields.
3. Leave the recipe as a draft until the physical output, material setup and yield have been verified. Check the explicit qualification box only after that verification. **Use as new revision** copies an existing definition for review without changing the prior revision or carrying its qualification forward.
4. Choose the qualified recipe and a matching completed batch. An operator or administrator enters accepted and rejected pieces for every main and flush output, then explicitly confirms physical inspection. Neither count is filled in automatically.
5. The application records an immutable inspection receipt and, for accepted pieces, a prepared Square addition. Accepted plus rejected must match each recipe yield. The action does not overwrite a Square count, fulfill an order or start another print.

Map every accepted output to its Square variation/location using the existing Square panel before inspection. Rejected-only batches create a receipt without a stock addition. Include sellable display pieces in the accepted count when physically present. Do not infer physical stock from a recorded Square quantity.

## Eligibility and limits

The first supported case is a finished single-print product. The exact payload hash, plate, model and printer configuration must match the recipe revision. The print needs one retained dispatch receipt, a completed correlated physical observation, a retained physical birth nonce and matching quiesced sender evidence. Unknown legacy jobs, failed/cancelled prints, quantity greater than one and jobs with multiple attempts are refused. Existing Education records remain outside this generic business workflow.

Known multi-part/component BOMs and products requiring additional consumables are refused until a finishing/assembly workflow exists. A qualified flush product may have its own single library model; that BOM model is frozen into the recipe and must remain unchanged when the batch is inspected. An empty BOM is not proof that a product needs no finishing: the qualifying administrator still attests that these are finished products. Material requirements are an explicit human-reviewed specification; this slice does not schedule recipes or independently certify the actual material loaded.

Recipes are immutable. A changed recipe is a new revision. Product deletion is refused when a retained recipe refers to it; organization deletion is refused while it has inspection receipts. This preserves identity/deduplication evidence instead of silently deleting business history. Job deletion does not delete receipts. Open **Inspection history and Square recovery** to search recipe/product/job text, filter prepared/unknown/confirmed additions and page through all receipts in chronological order (25 per page). Only additions linked to the displayed receipts are returned. The history refreshes after a new inspection or parent reload; there is no polling.

## Recovery and Square boundary

An identical inspection retry returns the same receipt. Different counts for the same job are refused for review. The receipt/idempotency identity is derived from the physical lifetime and dispatch claim, so restoring a backup cannot silently assign a new request key to that same print.

Prepared additions retain the account/environment, connector revision, exact variation/location snapshot, timestamp, quantities and idempotency key. They are additions (`NONE` to `IN_STOCK`), never physical-count overwrites. Existing additions created before connector-revision capture are retained and blocked for review, never silently upgraded or recreated.

The local delivery executor binds every saved change to its inspected receipt and current product mappings, validates merchant/location/tracking with read-only Square calls, rechecks configuration under the existing transaction locks and atomically reserves only a never-attempted prepared addition before crossing the network boundary. A second concurrent attempt cannot send it. Connector changes after that reservation cannot retarget the saved body/account; they cannot revoke an already-in-flight request. No production route or worker calls this executor, and the lowest HTTP layer refuses writes while the hard gate is false.

An uncertain outcome is never resent automatically. **Check Square history** reads up to three 100-entry pages of adjustment history after the receipt's timestamp minus one day. Every saved adjustment must match exact reference, normalized timestamp/quantity, variation/location/state and unique remote adjustment identity; duplicates, partial history and absent results remain unknown. A match confirms the retained receipt locally without changing Square stock. If history is unavailable or exceeds that bound, retain the receipt for manual review; absence is not proof of failure. **Check saved destination** validates account, active location, mapping and tracking for a prepared addition without sending it. Confirmed additions need no connector or network checks.

Square's successful batch-change response returns calculated counts, which are totals, not the quantities added. The transport accepts only an HTTP200 response without errors and complete valid count coverage for the saved variation/location pairs. Timeout, malformed/partial response or non200 leaves the durable operation unknown. Tests use simulated HTTP exclusively; this is not live-write certification. The older injected `stock_delivery.deliver` is simulation-only and is not used by the real executor.

Before enabling real writes, obtain owner approval, complete physical baseline and real recipe qualification, review activation/authorization and prove a small real batch. Current public idempotency documentation gives no Inventory-specific durable retention guarantee, so recovery does not rely on replaying an uncertain request indefinitely. Sources: [BatchChangeInventory](https://developer.squareup.com/reference/square/inventory-api/BatchChangeInventory), [inventory history](https://developer.squareup.com/reference/square/inventory-api/batch-retrieve-inventory-changes), [idempotency](https://developer.squareup.com/docs/build-basics/common-api-patterns/idempotency).

## Data and restore compatibility

Migration035 adds three empty side tables without changing old migration bytes or ORM baseline tables. They are included in the backup inventory. Released034 PostgreSQL archives are validated by the pinned, isolated `preserved034` source snapshot, upgraded in an owned disposable database and returned to the ordinary current validation path. Older frozen restore packages remain unchanged. Never delete a receipt to get around a duplicate/conflicting inspection.
