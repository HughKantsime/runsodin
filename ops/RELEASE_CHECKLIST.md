# O.D.I.N. Release Checklist

## Pre-Release (Local)

- [ ] Code changes committed and pushed to main
- [ ] Container running: `make build`
- [ ] Health checks pass: `make verify`
- [ ] Tests pass: `make test`
- [ ] Manual smoke test in browser if UI changes (http://localhost:8000)

## Tag & Publish

- [ ] Bump + push: `make release VERSION=1.3.XX`
- [ ] Verify GHCR workflow completes (GitHub Actions → green check)
- [ ] Verify image exists: `docker pull ghcr.io/hughkantsime/odin:v1.3.XX`

## Production (as end user)

- [ ] SSH to prod: `ssh root@YOUR_SERVER_IP`
- [ ] Pull and restart:
  ```bash
  cd /opt/odin/runsodin/runsodin
  # Update image tag in docker-compose.yml if pinning
  docker compose pull
  docker compose up -d
  ```
- [ ] Verify in browser: http://YOUR_SERVER_IP:8000
- [ ] Check container health: `docker inspect odin --format '{{.State.Health.Status}}'`

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
2. GHCR workflow only triggers on tags (not branch pushes). No tag = no image.
3. `:latest` is a moving target. Pin to version tags for production.
4. All 6 supervisord services should show RUNNING (monitors sleep+retry when no printers configured).
