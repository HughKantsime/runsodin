# Recipes and inspected production batches

The Products panel records fixed-yield physical inspections. Square remains the finished-stock authority. Acceptance prepares immutable additions locally; an explicit interactive operator/admin Send action posts one saved addition only when both activation keys are on. There is no auto-send, worker or resend path. See [Square activation](SUBLAB_SQUARE_READONLY.md).

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

Recipes are immutable. A changed recipe is a new revision. Product deletion is refused when a retained recipe refers to it; organization deletion is refused while it has inspection receipts. This preserves identity/deduplication evidence instead of silently deleting business history. Job deletion does not delete receipts. Open **Inspection history and Square recovery** to search recipe/product/job text, filter prepared/unknown/confirmed/withdrawn additions or the receipt disposition counted in baseline and page through all receipts in chronological order (25 per page). Only additions linked to the displayed receipts are returned. The history refreshes after a new inspection or parent reload; there is no polling.

## Recovery and Square boundary

An identical inspection retry returns the same receipt. Different counts for the same job are refused for review. The receipt/idempotency identity is derived from the physical lifetime and dispatch claim, so restoring a backup cannot silently assign a new request key to that same print.

Prepared additions retain the account/environment, exact variation/location snapshot, timestamp, frozen send quantities and idempotency key. The revision is a transaction fence, not permanent destination identity. Unrelated mappings, same-account token rotation and activation edits between attempts leave the destination valid. They are additions (`NONE` to `IN_STOCK`), never physical-count overwrites. Legacy additions derive identity from the stored destination and request. Their missing baseline version is null and matches only a product with no current baseline.

The local delivery executor binds every saved change to its inspected receipt and current product mappings, validates merchant/location/tracking with read-only Square calls, rechecks configuration under the existing transaction locks and atomically reserves only a never-attempted prepared addition before crossing the network boundary. A second concurrent attempt cannot send it. Connector changes after that reservation cannot retarget the saved body/account; they cannot revoke an already-in-flight request. The Send route calls this executor; both the writer and lowest HTTP mutation transport require the server switch and a strict True tenant flag. The executor runs write-scope preflight itself before reservation, so direct calls cannot bypass it.

An uncertain outcome is never resent automatically. **Check Square history** reads up to three 100-entry pages of adjustment history after the receipt's timestamp minus one day. Every saved adjustment must match exact reference, normalized timestamp/quantity, variation/location/state and unique remote adjustment identity; duplicates, partial history and absent results remain unknown. A match confirms the retained receipt locally without changing Square stock. If history is unavailable or exceeds that bound, retain the receipt for manual review; absence is not proof of failure. **Check saved destination** validates account, active location, mapping and tracking for a prepared addition without sending it. Confirmed additions need no connector or network checks.

Square's successful batch-change response returns calculated counts, which are totals, not the quantities added. The transport accepts only an HTTP200 response without errors and complete valid count coverage for the saved variation/location pairs. Timeout, malformed/partial response or non200 leaves the durable operation unknown. Tests use simulated HTTP exclusively; this is not live-write certification. The older injected `stock_delivery.deliver` is simulation-only and is not used by the real executor.

Before activating real writes, obtain owner approval, complete physical baseline and real recipe qualification, review activation/authorization and prove a small real batch. Current public idempotency documentation gives no Inventory-specific durable retention guarantee, so recovery does not rely on replaying an uncertain request indefinitely. Sources: [BatchChangeInventory](https://developer.squareup.com/reference/square/inventory-api/BatchChangeInventory), [inventory history](https://developer.squareup.com/reference/square/inventory-api/batch-retrieve-inventory-changes), [idempotency](https://developer.squareup.com/docs/build-basics/common-api-patterns/idempotency).

## Baseline membership and cutover review

Inspection uses the bound physical observation's `ended_at`, never the job's operator-entered `actual_end`. Legacy naive observation times are normalized as UTC, matching server recording conventions; aware times are converted to UTC. Live production-row verification was not performed in this offline build. Missing, invalid or conflicting evidence blocks acceptance for review.

For each positive output with a mapping baseline, an observation ending strictly after the baseline is sendable. Otherwise the operator must explicitly select **Counted in baseline** (`baseline_disposition: "in_baseline"`) or **Not counted, send** (`"send"`); there is no default. A conflicting counted choice for a post-baseline observation is refused. The receipt freezes disposition, send quantity, normalized observation time and the exact baseline UUID, at, quantity and attested_by. Without a baseline, baseline_version is null. Identical inspection replay returns the saved receipt before consulting current baseline policy.

Baseline-only receipts create no addition and remain visible through the counted-in-baseline history filter. Mixed receipts post only frozen send quantities. `in_baseline` is an output disposition, never a delivery state.

Setting/replacing a baseline puts any prepared addition with an older frozen baseline version behind `blocked: baseline_review`, without remote requests. This includes legacy/null versions. Interactive admins resolve one whole addition with `POST /api/products/batches/additions/{id}/baseline-review`, body `{"revision":"<current CAS revision>","answers":{"101":"not_counted"}}` (keys are every posted ODIN product id). `not_counted` attests **Not counted in baseline, send**; the append-only audit binds every current product baseline version. Replacing any baseline invalidates that review. The original receipt and request never change.

For **Counted in baseline**, use `"counted"` for every posted product. The same fenced transaction requires prepared/0, appends its audit and moves to terminal `withdrawn_baseline`. Checking a withdrawn addition returns immediately, without connector validation or remote reads. Neither review nor reconciliation can revive it. Mixed multi-product additions require the same answer for every posted product; missing or mixed answers remain blocked. Partial withdrawal and request splitting are unsupported. The connector CAS serializes baseline persistence, withdrawal, review binding and reservation, including owner 0.

For Cabinet Latch, externally inspected job 55 belongs inside the owner's 300 baseline: leave it unaccepted or explicitly counted in baseline. Job 58 is sendable if its bound observation ends after the baseline time. Owner approval is still required separately for rollout and each live send until standing approval is granted.

## Data and restore compatibility

Migration035 adds three empty side tables without changing old migration bytes or ORM baseline tables. They are included in the backup inventory. Released034 PostgreSQL archives are validated by the pinned, isolated `preserved034` source snapshot, upgraded in an owned disposable database and returned to the ordinary current validation path. Older frozen restore packages remain unchanged. Never delete a receipt to get around a duplicate/conflicting inspection.


## Prints started outside ODIN

`POST /api/products/production/outside-prints/{print_job_id}/bind` accepts a qualified `recipe_id` and the owner's explicit confirmation of outside start and file revision. It requires password-session or explicit `write:production` operator authority plus the `products.outside_prints` action. The completed print's printer must belong to the owner and match the recipe configuration. An ODIN receipt, Education ownership, previous binding or existing physical acceptance prevents binding.

The binding is retained as `outside_print_binding:<print_job_id>` in SystemConfig with an atomic audit record. It does not rewrite economic ownership history or claim ODIN dispatched the print. A false heuristic job match is detached; held, never-dispatched completed work is restored to pending and held, with completion times and attempt credit cleared. Future heuristic matches retain their previous state for correction. Inspect bound entries from the production job list using `print_job_id` in place of `job_id`. All yield, baseline, prepared-addition and explicit-send rules remain the same.

### Qualify the first outside plate

An administrator signed in with a password session can select **Bind an outside print → Qualify a new recipe from this print**. Choose a recent completed physical print (which also selects its printer), or enter its physical print ID and observed printer. Enter the plate number, material/color setup and fixed main/flush yields. Confirm both that the print started outside ODIN and its exact file revision. ODIN matches the observed filename against tenant-owned print files; if missing or ambiguous, supply the exact print file ID. When filename candidates exist, an explicit ID must be one of them. The file must retain a SHA-256 hash and belong to a generic, tenant-owned library model. Plate numbers in the UI start at 1; API `plate_index` starts at 0.

`POST /api/products/production/outside-prints/{print_job_id}/qualify-and-bind` accepts `printer_id`, optional `print_file_id`, `plate_index`, `name`, `materials`, `outputs`, `qualification_confirmed: true`, and the existing outside-print `attestation`. It uses the `products.recipes` permission with an independent admin and interactive-session requirement. API tokens cannot qualify outside-print recipes. The immutable recipe, `qualified_from_outside_print` flag, confirming user/time/print evidence (including `matched_by: explicit|filename`, observed filename and job name), audit and binding commit together. Running, bound, foreign, Education and ODIN-dispatched prints are refused. An uncorrelated dispatch receipt on the same printer within the observed start/end window also refuses qualification pending investigation. Existing inspection and Square send rules still apply.

### Scoped cancellation and recovery reads

`POST /api/jobs/{job_id}/cancel` accepts `write:jobs` (including broad `write`) alongside the existing admin/agent scope gates and `jobs.cancel` permission. Tokens whose cancellation authority comes only from `write:jobs` or broad `write` may cancel only pending jobs or held scheduled jobs with no dispatch receipt, active claim, sender registration or bound observed print. Education jobs remain excluded. The audit records the user and token ID. This action cancels queued work; it sends no printer command. Password sessions and tokens with `admin` or `agent:write` retain their previous `jobs.cancel` behavior, including cancellation of scheduled unheld jobs. Role permissions still apply, and the endpoint still refuses running or terminal jobs.

`GET /api/printers/{printer_id}/dispatch-recovery` accepts `read:printers` or broad `read`. Existing accessible printers with no visible retained evidence return HTTP 200, `state: investigation_required`, `refresh_required: false`, disabled write actions and ownership-investigation guidance. Absent and inaccessible claims use the same neutral response to avoid disclosing another organization’s claim. Global printers explicitly require investigation of missing organization ownership. Only unavailable telemetry sets `refresh_required: true`, with an explicit GET endpoint to use after fresh monitor updates. Missing/inaccessible printers still return 404. Recovery writes still require admin authority and printer-write scope.


### Delegated ODIN-dispatched recipe qualification

`POST /api/products/production/recipes` accepts an admin/operator API token with
explicit `write:production` and the `products.recipes` action. Password sessions
remain admin-only. Broad `write`, `admin` and agent scopes alone do not delegate
this operation. A token must send `qualification_confirmed: true` after the owner
confirms the physical count. It must choose a completed, single-run ODIN job with
an exact dispatch receipt, physical observation and stable configuration key;
a job already used as a recipe reference is refused for token qualification.
Tenant, Education and retained-evidence checks remain enforced. The immutable
recipe records `qualified_by_token`, `operator_id` and `api_token_id`; an atomic
`production.recipe_qualified` audit records the operator and token ID. This does
not add stock. Inspection and the separately gated stock send remain required.
Credentials, connectors, baselines, Square settings and outside-print
qualification still require password sessions.

### Explicit AMS source IDs

Reprint, schedule and job PATCH mappings use the file's one-based filament `slot`
as `source_slot`, and the printer's one-based tray as `target_slot`. File filament
4 to tray 4 is `[{"source_slot":4,"target_slot":4}]`, stored as `[-1,-1,-1,3]`.
Unused vector positions are `-1`. Previews and suggestions use the same file IDs.
A complete legacy positional mapping is accepted only when every file position
is `slot - 1`; sparse or reordered files cannot use positional numbering.
Missing, duplicate or invalid file IDs are refused. Explicit dispatch still
requires one verified source and plate; conflicting XML, plate JSON, material
profiles or gcode source commands refuse before any command is sent.
