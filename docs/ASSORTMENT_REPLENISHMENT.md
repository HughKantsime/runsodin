# Show assortment and advisory replenishment

Open **Products → Show assortment and replenishment**. The panel is closed initially and does not poll. It reads saved policy when opened; Square is read only when an operator/admin clicks **Check replenishment**. Nothing creates a job, starts a printer or changes Square stock.

## One-time explicit classification

An interactive administrator chooses an owned product, its assortment status and three numbers, then saves. Unconfigured products have no assumed active policy. Policy revisions use the existing tenant-scoped SystemConfig with compare-and-swap; concurrent/stale changes are refused rather than overwriting another product's policy. Policy saves are audited in the same transaction.

- **Active:** eligible for routine show-stock suggestions.
- **Trial:** deliberate manual trial, not automatic replenishment.
- **Inactive:** retain stock, files, receipts and orders; do not suggest routine replacement.

Do not use inactive to cancel a paid order or hide a wholesale commitment. This slice does not change order priorities or the scheduler.

## Three numbers per product

- **Print more when stock falls below** (reorder point): a recorded count at or above this number asks for nothing.
- **Print up to** (target): the stock level to refill to. Set it equal to the reorder point to print one batch at a time.
- **Smallest batch worth printing**: the fewest main pieces a plate must make before it is suggested or qualified for this product. Small incidental flush outputs are exempt. Every main output on a mixed recipe must satisfy its current policy before the recipe is offered.

"Print up to" cannot be lower than the reorder point. No number is assumed for a new product and no product is classified by name.

Policies saved before these numbers existed stored a production category instead. They keep working unchanged: such a policy reads back with the numbers its category produced (reorder point 5, or 15 for the two large categories; smallest batch 1, or 5 for the multicolor category; target equal to the reorder point). The first save writes the numbers out and the category is dropped.

For example, recorded stock of two with a qualified five-piece plate offers that fixed plate; it does not rebuild a three-piece deficit plate.

Draft recipes can be below the smallest batch, but cannot be qualified under it. Existing recipe revisions remain immutable and are filtered against current policies when suggesting plates. Recording actual inspected output is never blocked merely because assortment policy changed: real sellable pieces still need an accurate receipt.

## What a check establishes

The preview validates the connected merchant, active location, mapped variation, tracking and fresh recorded Square count. Product/connector/policy authority is reread after remote checks. Unknown, negative or fractional quantities require review; no shortage is inferred. These are recorded counts, not physical inventory certification.

Below the reorder point, the preview offers eligible qualified main-output recipe options with fixed yields, printer and materials. It never chooses a supposedly best printer from availability, credits expected output as stock or promises material/physical readiness.

Before offering another plate it checks prepared/unknown Square additions for accepted pieces and existing owner-scoped matching jobs. Pending, submitted, scheduled, printing, paused and completed-but-uninspected jobs suppress suggestions. Missing file/preparation/printer evidence is uncertainty, not free capacity; matching work on another printer also suppresses duplicates. Truncated scans require review rather than assuming no work. Failed/cancelled/rejected jobs do not count as successful production. Existing inspection identities distinguish retained accepted output from uninspected work.

Checks are snapshots. Paid orders, promised wholesale deadlines, materials, maintenance and a fresh clear-plate confirmation must be considered before any future dispatch. Previewing does not authorize a physical start. This slice does not schedule, automatically replenish, send notifications, buy labels or publish products.

## Persistence and limits

The policy holds no stock balance. Rack-to-travel-bin moves do not change Square totals. Policy cleanup shares existing transactional product/tenant deletion and retained-history guards, so a reused product ID cannot inherit a deleted product's policy. No schema or frozen restore package changes were made.

The panel shows up to 1,000 owned products and explicitly reports truncation. Recipe and pending-addition scans are bounded to 1,000 records, matching-job scans to 200. Incomplete relevant scans suppress suggestions. No real product classification, stock update or deployment was performed during local tests.
