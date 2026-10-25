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

## Publication authorization October25
Shane explicitly replied "Approved" to publishing, deploying and enabling this change on claude-operator. Release2.0.15 keeps the separate2.0.14 work out. Follow exact-candidate Trusted Validation, publication and production promotion; then grant scope and verify no stock write.

### Release preparation
Version2.0.15 metadata prepared. Corrected release-tool minLength8 to7 in schema and workflow validator so Shane's exact "Approved" can be recorded, without inventing approval text or requesting it again. Fresh design/diff review passed;115 release authorization/evidence/mutation tests passed. All actor, candidate, evidence, nonce, expiry and environment controls unchanged.

### First Trusted Validation outcome
Run40195870763 candidate6d7dd25d stopped atCV01:313/314 release-control tests passed, one failure because legal Last Updated headers saidOctober24 while release changelog saidOctober25. No publication/deploy. Corrected only those two headers; all314 local release-control tests now pass, installer smoke pending. One-token grant script independently reviewed; explicit checks (not optimizable assert side effects), normal/-O repeat/identity/audit/hash/expiry tests pass.

### Publication metadata blocker and fix
Candidate1888be91 passed all10 Trusted Validation gates (run40196412923;12,997 contract tests) and stage eligibility40200158877. Publication40200211879 stopped before registry login/mutation because the current release lacked an entry in released_post027_ledgers.json. Official record_post027_ledger.py appended2.0.15 with the exact unchanged7 migration IDs used by2.0.13. Exact publication ledger preflight and host amd64 emulation pass; independent review found no blockers. Added a fast CV01 test requiring VERSION ledger equality, so this cannot wait until publication again. No image published, no deployment/token grant. New immutable candidate requires fresh full validation under current pipeline; prior successful evidence is retained, not represented as evidence for the new SHA. Refresh host backup immediately before deployment if the current snapshot ages out.

### Bounded CI fixture correction
Run40200647935 atc7b8d488 was cancelled after one contract failure: test_preserved027_builder oversized child hit PermissionError/EPERM in existing parent os.killpg cleanup after the expected output-limit exception. Production supervisor and frozen bundle are unchanged from the prior all10-passing1888be91; six focused modes passed on reproduction. Independent diagnosis did not prove exact Darwin cause and identified the exited-child EPERM cleanup path as an unresolved pre-existing issue; do not claim it fixed. Refined only the live oversized-output fixture to flush20k then stay live60s, require the specific output-limit error, and assert SIGKILL returncode plus existing reaped/closed-pipe guarantees. Full module31 tests and25 independent oversized cases pass. Fresh review approved this stronger live-child test. No production-code change, no test skips or evidence rewriting. New candidate must pass the complete pipeline; retain prior failure evidence.
