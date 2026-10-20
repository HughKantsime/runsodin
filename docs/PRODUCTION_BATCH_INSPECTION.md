# Recipes and inspected production batches

This local development slice adds a closed-by-default panel under **Products**. Square remains the finished-stock authority. The application can prepare additions but **cannot send them**: no delivery endpoint, automatic worker or production write transport is installed.

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

Recipes are immutable. A changed recipe is a new revision. Product deletion is refused when a retained recipe refers to it; organization deletion is refused while it has inspection receipts. This preserves identity/deduplication evidence instead of silently deleting business history. Job deletion does not delete receipts. The panel displays up to 100 inspection receipts in stable identity order; the database retains all receipts. A searchable history view remains future work.

## Recovery and Square boundary

An identical inspection retry returns the same receipt. Different counts for the same job are refused for review. The receipt/idempotency identity is derived from the physical lifetime and dispatch claim, so restoring a backup cannot silently assign a new request key to that same print.

Prepared additions retain the account/environment and exact variation/location snapshot, timestamp, quantities and idempotency key. They are additions (`NONE` to `IN_STOCK`), never physical-count overwrites. The development recovery service accepts an injected simulated transport: intent is durable before delivery, ambiguous outcomes remain unknown, and retries reuse exactly the saved bytes/key. Confirmed requests are not sent again, and an older timeout cannot downgrade a concurrent confirmation. There is no live Square transport in this release slice. No second stock balance is kept.

Before enabling real writes, independently review and implement the Square transport, live mapping/tracking revalidation, authorization and recovery controls, then obtain owner approval and prove a small real batch. A physical baseline and real recipe qualification are also still required. This document does not certify the broader plate-to-stock workflow as complete.

## Data and restore compatibility

Migration035 adds three empty side tables without changing old migration bytes or ORM baseline tables. They are included in the backup inventory. Released034 PostgreSQL archives are validated by the pinned, isolated `preserved034` source snapshot, upgraded in an owned disposable database and returned to the ordinary current validation path. Older frozen restore packages remain unchanged. Never delete a receipt to get around a duplicate/conflicting inspection.
