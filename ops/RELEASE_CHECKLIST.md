# O.D.I.N. Release Checklist

> **Current state:** local foundation only. Publishing and production promotion
> are disabled until the reviewed successor workflows are installed.

## Candidate Preparation (Local)

- [ ] Code changes committed locally
- [ ] Container running: `make build`
- [ ] Health checks pass: `make verify`
- [ ] Trusted aggregate passes: `make trusted-validation-gate`
- [ ] Manual smoke test in browser if UI changes (http://localhost:8000)

## Immutable Candidate Evidence

- [ ] Create the version commit with `make bump VERSION=X.Y.Z`
- [ ] Confirm the local helper created no tag and performed no network write
- [ ] After remote bootstrap exists, manually dispatch `Trusted Validation` for
      the protected `release-candidate/<40hex>` ref and matching SHA
- [ ] Retain the aggregate JSON, JUnit, and HTML evidence

## Publish and Production

- [ ] Stop here until the immutable-evidence and production-promotion specs are
      implemented, reviewed, and explicitly authorized at their ship gates

## Rollback (if needed)

**Canonical procedure: `ops/RUNBOOK.md` §4.4.** That is the source of
truth — patch the runbook (and run the §4.6 drill) rather than this
checklist if the rollback path drifts.

```bash
# On prod — pin to the previous-known-good semver tag (e.g., v1.9.3).
# Path is the directory containing your install/docker-compose.yml.
GOOD_TAG=v1.9.3
docker pull ghcr.io/hughkantsime/odin:${GOOD_TAG}
sed -i "s|image: ghcr.io/hughkantsime/odin:.*|image: ghcr.io/hughkantsime/odin:${GOOD_TAG}|" \
    docker-compose.yml
docker compose down && docker compose up -d
curl -fsS http://localhost:8000/health    # confirm rolled-back version
```

## Known Gotchas

1. **NEVER** use `build:` in production compose.
2. Local version helpers never tag or push.
3. `:latest` is a moving target and is not candidate evidence.
4. All 6 supervisord services should show RUNNING (monitors sleep+retry when no printers configured).
