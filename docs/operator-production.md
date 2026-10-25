# Delegating production inspections

A password-authenticated admin or operator can create a per-user token with explicit `write:production`. Its owner must still have operator/admin role at use time. Include `read` for the production overview, recipes, general `/api/jobs` reads and Square stock reads. The older `/api/products/production/jobs` eligibility list additionally requires generic `write`; the existing claude-operator token already has it. Existing job/upload/printer scopes are separate. Generic `write`, `admin` and `agent:write` do not imply this explicit permission.

Permitted writes:

- `POST /api/products/production/inspections`
- `POST /api/products/production/additions/{addition_id}/check`
- `POST /api/products/batches/additions/{addition_id}/send`

This does not grant credential creation, Square connection changes, stock activation, mapping/baseline changes, recipe qualification, assortment configuration or finishing. Those retain their existing password-session requirements. Configured action access, tenant ownership and the operator role floor still apply. A global API key does not qualify.

## Operator procedure

1. Obtain the owner's accepted/rejected counts and disposition. Do not infer a good count from a completed print.
2. Read production history first and locate the job. If a receipt already exists, use its addition rather than record it again.
3. Read the current connection revision and exact qualified recipe. Submit the inspection with `inspection_confirmed: true`, whole accepted/rejected counts and the applicable baseline disposition. This only prepares an immutable addition.
4. Locate the addition by receipt ID, check it, then explicitly send it if authorized. Both server and tenant activation keys must already be enabled by an interactive admin.
5. Verify addition `state: confirmed` and read Square stock. Report the actual result. The immutable receipt/idempotency key and reservation prevent duplicate stock on retry.
6. For an unknown outcome, check the existing addition. Do not create another receipt or send again. Reconciliation matches Square adjustment history; unresolved outcomes need human review.

Inspection creation and send reservation stage audit records in the same transaction, with operator ID, authentication kind and nonsecret token ID. Token values belong only in the user's secret store.

## Existing claude-operator credential

The October25 credential lacks this scope until the change is deployed and an interactive admin grants it. Do not patch the running container or use the token to grant itself authority. Preserve the existing Keychain credential, hash, expiry and other scopes when applying an owner-approved scope update. Job71 is already recorded and Square was600 at the prior verification; never replay it as a test. Automated tests use isolated databases and synthetic Square transport, not the live business inventory.
