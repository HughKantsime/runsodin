# O.D.I.N. Release Checklist

> **Current state:** manual validation, eligibility, immutable publication, and
> digest-preserving production promotion. Ordinary pushes never deploy.

## Candidate Preparation (Local)

- [ ] Code changes committed locally
- [ ] Container running: `make build`
- [ ] Health checks pass: `make verify`
- [ ] Trusted aggregate passes: `make trusted-validation-gate`
- [ ] Manual smoke test in browser if UI changes (http://localhost:8000)

## Candidate Evidence

- [ ] Create the version commit with `make bump VERSION=X.Y.Z`
- [ ] Confirm the local helper created no tag and performed no network write
- [ ] After remote bootstrap exists, manually dispatch `Trusted Validation` for
      the protected `release-candidate/<40hex>` ref and matching SHA
- [ ] Confirm the workflow produced the three-file sanitized `evidence/` bundle
- [ ] Verify `manifest.sha256` against the exact downloaded `manifest.json`
- [ ] Confirm all ten component summaries are passing and source SHA is exact

## Publish and Production

- [ ] Treat validation as evidence only; it is not promotion authorization
- [ ] Dispatch the distinct owner-only Promotion Eligibility workflow with the
      exact validation run ID, candidate SHA/ref, manifest digest, nonce, and
      verbatim authorization text
- [ ] Confirm its five-file decision artifact is schema-valid and `eligible`
- [ ] Require exact protected `production` environment approval evidence
- [ ] For publication, use a fresh `stage` eligibility decision and dispatch
      `Publish Immutable Image` with the exact candidate/version/evidence inputs
- [ ] Confirm its receipt reports both platform manifests passed and both
      immutable tags resolve to the same OCI index digest
- [ ] Renew/verify public TLS before production; promotion fails before mutation
      when TLS or current public health is invalid
- [ ] For production, create a fresh `production` eligibility decision (one-hour
      maximum), record explicit final ship authorization, and dispatch
      `Promote Image to Production` with the exact publication receipt digest
- [ ] Confirm the production receipt records prior `latest`, verified rollback
      tag, target digest, observed public version, and terminal success

## Rollback (if needed)

**Canonical procedure: `ops/RUNBOOK.md` §4.4.** That is the source of
truth — patch the runbook (and run the §4.6 drill) rather than this
checklist if the rollback path drifts.

Primary rollback uses the exact command in the corresponding successful production receipt. It
copies the receipt's verified `rollback-<run>-1` digest back to `latest`; no
source build or SSH is involved. Confirm registry `latest` matches the prior
digest, then verify public `/health` after Watchtower reconciles. Direct-host
semver pinning in `ops/RUNBOOK.md` §4.4 is break-glass only.

## Known Gotchas

1. **NEVER** use `build:` in production compose.
2. Local version helpers never tag or push.
3. `:latest` is a moving production pointer, never candidate evidence.
4. Publication and production share one non-cancelling mutation lock.
5. All 6 supervisord services should show RUNNING (monitors sleep+retry when no printers configured).
