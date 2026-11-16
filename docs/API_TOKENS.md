# API tokens: Square setup

Square setup permission (`write:square_setup`) is off by default. Upgrading ODIN grants it to nobody. A signed-in administrator can create a named personal token in Settings → Access → API Tokens, then choose **Grant Square setup** on that token's list entry. **Revoke Square setup** removes this permission immediately for subsequent requests and keeps the token's other permissions. Both changes are audited with the administrator and token identity. Tokens cannot grant or revoke this permission themselves.

The token's owner must still be an administrator and have the **Manage Square** permission (`products.square_manage`). This does not give operators administrator access. Broad `write`, `admin`, `agent:write` and `write:production` scopes do not include Square setup. Camera-only tokens cannot receive it. The existing Products license requirement applies; this permission cannot be granted or used while Education mode is enabled. Revocation remains available.

Use the token in the `X-API-Key` header. This permission allows exactly these two operations (also available under `/api/v1`):

- `PUT /api/products/{product_id}/square`: save a product's Square variation and location mapping.
- `PUT /api/products/{product_id}/square/baseline`: record the product's stock baseline in ODIN.

Every token call must include `approval_note`, 1–500 characters, naming who approved, where and when. For example: `"Approved by the inventory owner in work order 123 on 2026-11-16"`. The approval note is a record, not a verification. ODIN does not check the approval. The audit row records the note, API-token authentication, token ID, token name and token owner's user ID. Signed-in session calls do not need the note.

Neither route writes to Square. Mapping reads Square to validate the location and stock tracking. Baseline recording changes only ODIN's saved record and makes no Square request. The previous baseline is retained in the audit row; there is no true undo. Unknown additions block a baseline change. Prepared additions remain pending and need a signed-in administrator's review before sending against the new baseline. Stale connection revisions are refused. ITAR restrictions on Square reads remain in effect.

Connecting, replacing or disconnecting Square credentials, changing the stock-write activation switch, removing a mapping and reviewing an addition's baseline still require a signed-in administrator. This permission grants no access to users, billing, authentication or stock sending. Tokens need separate existing read permissions to look up products or Square connection revisions. Delegated setup calls use the existing rate limiter, with a separate allowance of 30 requests per minute per token per route.
