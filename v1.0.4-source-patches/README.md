# O.D.I.N. v1.0.4 Source Patches

## Bugs Fixed

### Bug 1: 401 Unauthorized on Test Connection, AMS Sync, File Upload
**Root cause:** `frontend/src/pages/Printers.jsx` defines `apiHeaders` with only
`X-API-Key` (which is `undefined` since `VITE_API_KEY` is never set). The JWT
Bearer token is never sent on raw `fetch()` calls. Same issue in `api.js` for
`printFiles.upload` and `licenseApi.upload`.

**Fix (frontend):** Replace static `apiHeaders` with `getApiHeaders()` function
that includes both X-API-Key (if configured) and Bearer token from localStorage.

**Fix (backend defense-in-depth):** Modify `get_current_user` to accept
X-API-Key as fallback auth, so even unpatched frontends can authenticate.

### Bug 2: 500 on /api/stats and /api/print-jobs on fresh installs
**Root cause:** `print_jobs` table is created via raw SQL (not in SQLAlchemy
models), but the migration never runs during Docker init. `Base.metadata.create_all()`
only creates tables defined in models.py.

**Fix:** Add `CREATE TABLE IF NOT EXISTS print_jobs` block to `docker/entrypoint.sh`
after the existing users table creation block.

## Files Modified
- `backend/main.py` — get_current_user X-API-Key fallback
- `frontend/src/pages/Printers.jsx` — getApiHeaders() with Bearer token  
- `frontend/src/api.js` — printFiles.upload & licenseApi.upload auth
- `docker/entrypoint.sh` — print_jobs table creation

## Usage
```bash
cd /path/to/odin-repo
python3 v1.0.4-source-patches/patch_all.py
git diff  # review changes
echo "1.0.4" > VERSION
git add -A && git commit -m "fix: v1.0.4 - auth 401 on test-connection/AMS/upload + missing print_jobs table"
docker compose build && docker compose up -d
```
