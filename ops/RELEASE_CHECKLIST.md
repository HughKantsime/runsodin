# O.D.I.N. Release Checklist

## Pre-Release (Sandbox .70.200)

- [ ] Code changes committed and pushed to main
- [ ] Run full sandbox pipeline:
  ```bash
  ./ops/deploy_sandbox.sh
  ```
  This runs: Docker build → Phase 0 → Pytest (Phases 1-3)
- [ ] All 911+ tests pass, 0 failures
- [ ] Phase 0 shows no FAILs
- [ ] Manual smoke test in browser (if UI changes)

## Tag & Publish

- [ ] Bump VERSION file: `echo "1.0.XX" > VERSION`
- [ ] Commit: `git add VERSION && git commit -m "release: v1.0.XX"`
- [ ] Tag: `git tag v1.0.XX`
- [ ] Push: `git push origin main v1.0.XX`
- [ ] Verify GHCR workflow completes (GitHub Actions → green check)
- [ ] Verify image exists: `docker pull ghcr.io/hughkantsime/odin:v1.0.XX`

## Production Deploy (.71.211)

- [ ] Run production deploy:
  ```bash
  ./ops/deploy_prod.sh v1.0.XX
  ```
  This runs: Pull → Restart → Phase 0 verification
- [ ] Phase 0 output shows:
  - [ ] Image is `ghcr.io/hughkantsime/odin:v1.0.XX`
  - [ ] No `build:` in compose
  - [ ] All required supervisord services RUNNING
  - [ ] All API endpoints return 200
  - [ ] VERSION matches expected
- [ ] Browser check: http://\<prod-ip\>:8000
- [ ] Deploy log entry written to `/opt/odin/deploy.log`

## Rollback (if needed)

```bash
# Pull the previous known-good tag
docker pull ghcr.io/hughkantsime/odin:v1.0.PREVIOUS
# Update compose and restart
sed -i 's|image: ghcr.io/hughkantsime/odin:.*|image: ghcr.io/hughkantsime/odin:v1.0.PREVIOUS|' \
    /opt/odin/runsodin/runsodin/docker-compose.yml
cd /opt/odin/runsodin/runsodin && docker compose down && docker compose up -d
# Verify
./ops/phase0_verify.sh prod
```

## Known Gotchas

1. **NEVER** use `build:` in production compose. Phase 0 will catch this, but don't let it get there.
2. GHCR workflow only triggers on tags (not branch pushes). No tag = no image.
3. `:latest` is a moving target. Pin to version tags for production.
4. Optional supervisord services (`elegoo_monitor`, `moonraker_monitor`, `prusalink_monitor`) now stay alive with sleep+retry even when no printers of that type are configured. All 6 services should show RUNNING.
5. If `docker compose up` seems to ignore your image changes, check for Docker build cache: `docker compose build --no-cache` (sandbox only).
