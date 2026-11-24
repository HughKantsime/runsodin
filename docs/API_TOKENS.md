# API tokens

## Printer controls

A token with `write:printers` can request a pause, resume, or stop if its owner has permission to edit the printer. Broad `write` includes these controls. Pause and resume also accept `agent:write` or `admin`. Printer safety checks still apply. If an open fault blocks a resume request, a person must review the printer and resume the print from the ODIN web app.

## Square setup

Square setup permission (`write:square_setup`) is off by default. Upgrading ODIN grants it to nobody. A signed-in administrator can create a named personal token in Account → API Tokens, then choose **Grant Square setup** on that token's list entry and confirm. Only the owner can grant it, and only on an unexpired token. **Revoke Square setup** removes this permission immediately for subsequent requests and keeps the token's other permissions. Both changes are audited with the administrator and token identity. Tokens cannot grant or revoke this permission themselves.

In Account → API Tokens, administrators can also see and revoke Square setup grants for other users in their organization. Platform administrators can do this across organizations. The API includes these grants when a signed-in administrator requests `GET /api/tokens?managed_square_setup=true`. Demoting, deactivating or deleting a user removes the grant from every token and records each removal. Restoring administrator access requires a new grant.

To grant or use Square setup, the token's owner must be an administrator and have the **Manage Square** permission (`products.square_manage`). This does not give operators administrator access. Broad `write`, `admin`, `agent:write` and `write:production` scopes do not include Square setup. Camera-only tokens cannot receive it. The existing Products license requirement applies; this permission cannot be granted or used while Education mode is enabled. Revocation remains available.

Use the token in the `X-API-Key` header. This permission allows exactly these two operations (also available under `/api/v1`):

- `PUT /api/products/{product_id}/square`: map a product that has no Square mapping. Repeating the same variation and location returns success without changing anything. Changing an existing mapping requires a signed-in administrator (409 otherwise).
- `PUT /api/products/{product_id}/square/baseline`: record a first stock baseline in ODIN. Changing an existing baseline requires a signed-in administrator (409 otherwise). ODIN reads the live Square count and refuses a quantity that differs, an unavailable or untracked count, or a read blocked by ITAR mode. Nothing is changed on refusal.

For a token baseline, `at` must be no more than 15 minutes old and no more than 60 seconds in the future (422 otherwise). Times that cannot be represented in UTC are refused for both tokens and signed-in sessions.

Every token call must include `approval_note`, 1–500 characters, naming who approved, where and when. For example: `"Approved by the inventory owner in work order 123 on 2026-11-16"`. The approval note is a record, not a verification. ODIN does not check the approval. The note is stored exactly as sent, including surrounding whitespace. Notes containing a NUL, no visible characters, or text the credential redactor would change are refused (422). The audit row records the note, API-token authentication, token ID, token name and token owner's user ID. Signed-in session calls do not need the note.

Neither route writes to Square. Mapping reads Square to validate the location and stock tracking. Token baseline recording reads Square to check stock tracking and the live count. Signed-in baseline recording makes no Square request and still permits replacing a baseline, retaining its previous value in the audit row; there is no true undo. Unknown additions block a baseline change. Prepared additions remain pending and need a signed-in administrator's review before sending against the new baseline. Stale connection revisions are refused. ITAR restrictions on Square reads remain in effect.

Connecting, replacing or disconnecting Square credentials, changing the stock-write activation switch, removing a mapping and reviewing an addition's baseline still require a signed-in administrator. This permission grants no access to users, billing, authentication or stock sending. Tokens need separate existing read permissions to look up products or Square connection revisions. Delegated setup calls use the existing rate limiter, with a separate allowance of 30 requests per minute per token per route.

Delegated mapping audits include the location, variation, item and variation names, and a previous mapping of none. Grant and revoke audits include the scope lists before and after; unchanged grants and revocations add no audit row. ODIN checks the token and its owner again after the Square read, before saving, so a revocation during that read stops the change.

Refused setup requests from an authenticated token record the token ID, product ID and reason in the audit log. Repeated refusals for the same token, product and reason are recorded at most once per minute.

Without permission to read Square connection status, the reply includes the new or current revision and only the requested product's mapping IDs and stock tracking result. Repeating the same mapping skips the Square read, so the tracking result is unknown (`null`). A baseline reply includes its count, time and version. Sessions and tokens that can read the connection still receive the full reply.
