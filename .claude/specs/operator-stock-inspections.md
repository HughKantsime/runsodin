# Scoped production operator

## Outcome and authorization
Shane requests the operator path Claude suggested so claude-operator can record owner-confirmed production inspections and explicitly send their immutable stock additions through ODIN, without a password session. Job71 is already recorded and Square600; never replay it for testing. No release/push/deploy authorization is inferred.

## Design and acceptance
- New explicit scope write:production permits only inspection creation, addition status checks, and sending prepared additions through existing endpoints. The existing operator role floor, configurable RBAC actions, tenant ownership, physical evidence, immutable receipts/idempotency, baseline rules and two-key Square activation remain enforced.
- Password sessions retain current behavior. API tokens must explicitly possess write:production: generic write/admin alone must not silently authorize financial stock operations. Global API keys stay denied.
- Credentials, mappings, baselines, activation, recipes and finishing policies stay interactive-only. No global weakening of require_interactive_session.
- Add the scope to mint validation/UI and require operator+ for minting it. No secret values in artifacts.
- HTTP tests prove narrow token reaches real inspection/prepared-send lifecycle, no duplicate additions on retries; other scopes/viewer/global credentials refused; credential routes remain guarded. Existing Square/tenant/idempotency tests remain passing.
- Existing production token upgrade requires deployed support and a password-authenticated admin update; not a reason to patch live code or bypass credential guards.

## Current State
Isolated branch feat/operator-stock-inspections at origin/main d4aaded6. Fresh production /health reports2.0.13, matching this branch base. Separate release2.0.14 worktree is outside this change.

## Decisions
Use existing routes and explicit write:production scope. No new credential storage. No production stock mutation during development.

## Blockers
None for implementation. Publication/deployment require concrete approval after validation.

## Verification Evidence
Completed:165 security tests (including37 new operator cases),156 Square/production regression tests,10 token UI tests; frontend production build; staged gitleaks no leaks; git diff --check. Fresh independent diff review found no security blockers. Corrected read-access documentation and added audit-flush rollback evidence in response to advisories. Isolated fixture transport only; no live inventory mutations.

## Next action
Owner approval for push/publication/deployment; then grant explicit write:production to existing claude-operator via interactive admin authority, preserve credential and expiry, and verify read-only/admission behavior without replaying a completed batch.

### Design review resolution
Fresh design review identified exact-scope implication and an additional service-layer interactive check. Implementation will use one production-specific assertion shared by route dependency and execute_prepared, exact-membership special handling in token_scope_granted for write:production, and unchanged generic interactive guards. Permitted paths: POST /products/production/inspections, POST /products/production/additions/{id}/check, POST /products/batches/additions/{id}/send only. Keep the shared operator dependency unchanged for unrelated endpoints. Verify all other production/config/credential endpoints refuse production-only tokens; action policy, current role and tenant still apply. Reuse existing recovery/concurrency/activation/baseline tests and add scoped HTTP coverage for them. Never touch production data for verification.

## Final checkpoint October25
Implemented exact production scope and shared route/service admission, mint role floor, UI option, transactional actor audits and operator guide. No shared credential guard weakened. Job71 remains completed and must not be replayed. Code is locally reviewable; not pushed, published, deployed or enabled on the live credential. No production claim until release and token-grant evidence exist.
