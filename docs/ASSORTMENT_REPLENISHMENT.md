# Show assortment and advisory replenishment

Open **Products → Show assortment and replenishment**. The panel is closed initially and does not poll. It reads saved policy when opened; Square is read only when an operator/admin clicks **Check replenishment**. Nothing creates a job, starts a printer or changes Square stock.

## One-time explicit classification

An interactive administrator chooses an owned product, assortment status and production category, then saves. Unconfigured products have no assumed active policy. Policy revisions use the existing tenant-scoped SystemConfig with compare-and-swap; concurrent/stale changes are refused rather than overwriting another product's policy. Policy saves are audited in the same transaction.

- **Active:** eligible for routine show-stock suggestions.
- **Trial:** deliberate manual trial, not automatic replenishment.
- **Inactive:** retain stock, files, receipts and orders; do not suggest routine replacement.

Do not use inactive to cancel a paid order or hide a wholesale commitment. This slice does not change order priorities or the scheduler.

## Two different rules

The normal stock trigger is below five recorded pieces, including the sellable display piece. Jumbo and half-size dragons trigger below fifteen per design/product. These are triggers, not replenishment targets.

Multicolor character main batches have a separate minimum of five pieces per qualified plate, including trials. Other products have no new minimum above one. Small incidental flush outputs are exempt. Every main output on a mixed recipe must satisfy its current policy before the recipe is offered.

For example, recorded stock of two with a qualified five-piece character plate offers that fixed plate; it does not rebuild a three-piece deficit plate. Dragon color flexibility and existing qualified material requirements are preserved. No products are automatically classified by name.

Draft recipes can be below the batch rule, but cannot be qualified under an applicable multicolor policy. Existing recipe revisions remain immutable and are filtered against current policies when suggesting plates. Recording actual inspected output is never blocked merely because assortment policy changed: real sellable pieces still need an accurate receipt.

## What a check establishes

The preview validates the connected merchant, active location, mapped variation, tracking and fresh recorded Square count. Product/connector/policy authority is reread after remote checks. Unknown, negative or fractional quantities require review; no shortage is inferred. These are recorded counts, not physical inventory certification.

Below the trigger, the preview offers eligible qualified main-output recipe options with fixed yields, printer and materials. It never chooses a supposedly best printer from availability, credits expected output as stock or promises material/physical readiness.

Before offering another plate it checks prepared/unknown Square additions for accepted pieces and existing owner-scoped matching jobs. Pending, submitted, scheduled, printing, paused and completed-but-uninspected jobs suppress suggestions. Missing file/preparation/printer evidence is uncertainty, not free capacity; matching work on another printer also suppresses duplicates. Truncated scans require review rather than assuming no work. Failed/cancelled/rejected jobs do not count as successful production. Existing inspection identities distinguish retained accepted output from uninspected work.

Checks are snapshots. Paid orders, promised wholesale deadlines, materials, maintenance and a fresh clear-plate confirmation must be considered before any future dispatch. Previewing does not authorize a physical start. This slice does not schedule, automatically replenish, send notifications, buy labels or publish products.

## Persistence and limits

The policy holds no stock balance. Rack-to-travel-bin moves do not change Square totals. Policy cleanup shares existing transactional product/tenant deletion and retained-history guards, so a reused product ID cannot inherit a deleted product's policy. No schema or frozen restore package changes were made.

The panel shows up to 1,000 owned products and explicitly reports truncation. Recipe and pending-addition scans are bounded to 1,000 records, matching-job scans to 200. Incomplete relevant scans suppress suggestions. No real product classification, stock update or deployment was performed during local tests.
