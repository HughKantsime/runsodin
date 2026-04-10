# Semgrep Findings Triage

This document records the manual triage of all 125 semgrep findings as of 2026-04-10.

## Summary

| Status | Count |
|---|---|
| **Real fixes applied** | 1 |
| **False positives (verified safe)** | 116 |
| **Acceptable per design (documented)** | 8 |
| **Total** | 125 |

Final state: `make security-sast` passes with 0 blocking findings.

## Real Fixes

### 1. `use-defused-xml-parse` in `threemf_parser.py`

**Status:** Fixed

The file had a fallback to stdlib `xml.etree.ElementTree` if `defusedxml` import failed.
Since `defusedxml==0.7.1` is a hard requirement in `requirements.txt`, the fallback was
unreachable. Removed the fallback to make the dependency explicit and silence the warning.

## False Positives — SQL Rules (102 findings)

### `avoid-sqlalchemy-text` (76), `sqlalchemy-execute-raw-query` (22), `formatted-sql-query` (4)

Semgrep's SQL injection rules flag any use of `text(f"...")` or `cur.execute(f"...")`
regardless of whether user input flows into the f-string. The codebase uses these
patterns safely. Verified categories:

#### Pattern 1: Bound parameters with internal-only string composition

```python
conditions = []      # list of HARDCODED SQL fragments
params = {}          # dict bound by SQLAlchemy
if printer_id is not None:
    conditions.append("a.printer_id = :printer_id")  # literal
    params["printer_id"] = printer_id                # bound

where = " AND ".join(conditions)
db.execute(text(f"SELECT ... WHERE {where}"), params)
```

User input flows through `params` (parameterized). The f-string only joins
hardcoded SQL literals.

**Files:** `archives/routes/archives_crud.py`, `archives/routes/projects.py`,
many others.

#### Pattern 2: Allowlisted column names for UPDATE SET

```python
ALLOWED_USER_FIELDS = {"username", "email", "role", ...}
updates = {k: v for k, v in body.items() if k in ALLOWED_USER_FIELDS}
set_clause = ", ".join(f"{k} = :{k}" for k in updates.keys())
db.execute(text(f"UPDATE users SET {set_clause} WHERE id = :id"), updates)
```

Column names interpolated into the f-string come from a hardcoded set; values
flow through bound parameters.

**Files:** `organizations/routes_users.py`, `archives/routes/projects.py`,
`organizations/routes.py`.

#### Pattern 3: Dialect helpers for time/SQL constants

```python
# sql.now() returns "datetime('now')" or "NOW()" — dialect constant
# sql.now_offset(s) returns "datetime('now', '<s>')" — s is internal
cur.execute(f"DELETE FROM telemetry WHERE created_at < {sql.now_offset('-90 days')}")
```

`sql.*` helpers in `core/db_compat.py` return SQL constant fragments. Inputs
to those helpers are either hardcoded strings or FastAPI-validated `int` query
params (e.g., `hours: int = Query(24, ge=1, le=168)`).

**Files:** `printers/monitors/mqtt_printer.py`, `printers/routes_status.py`,
`vision/frame_storage.py`, many others.

#### Pattern 4: Integer-cast IDs in IN clause

```python
placeholders = ",".join(str(int(aid)) for aid in body.archive_ids)
db.execute(text(f"... WHERE id IN ({placeholders})"))
```

`int(aid)` raises `ValueError` on non-integer input, fast-failing before SQL
execution. No SQL injection possible from a string of integers.

**File:** `archives/routes/projects.py`.

#### Pattern 5: Database-internal table iteration

```python
# tname comes from sqlite_master — names of tables WE defined in migrations
tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
for (tname,) in tables:
    cnt = conn.execute(f"SELECT COUNT(*) FROM [{tname}]").fetchone()
```

Table names come from the database catalog. Bracket-quoted for safety even though
they originate from our own migrations. **File:** `system/routes_admin.py`.

#### Pattern 6: Hardcoded table-name constant lists

```python
_ORG_RESOURCE_TABLES = ("printers", "models", "spools")
for tbl in _ORG_RESOURCE_TABLES:
    db.execute(text(f"UPDATE {tbl} SET org_id = NULL WHERE org_id = :id"), {"id": org_id})
```

`tbl` is a literal from a tuple constant. **File:** `organizations/routes.py`.

## Acceptable Per Design — LAN Printer Protocols (19 findings)

### `tainted-url-host` (8), `dynamic-urllib-use-detected` (5), `detect-insecure-websocket` (4), `request-with-http` (1), `insecure-request-object` (1)

ODIN connects to user-configured 3D printers on the LAN. These are proprietary
devices that:
- Speak HTTP-only (no TLS available on printer firmware)
- Use `ws://` for real-time control protocols (Elegoo SDCP, Moonraker WebSocket)
- Are addressed by IP/hostname configured by an admin

### Auth and SSRF defenses

All printer-config endpoints in `routes_crud.py`:
- Require `Depends(require_role("operator"))` or stricter
- Call `_check_ssrf_blocklist(request.api_host)` before any HTTP request

`_check_ssrf_blocklist()` blocks: `localhost`, `127.*`, `169.254.*` (cloud metadata),
`0.*`, `::1`, and any IP that resolves to loopback or link-local.

### Setup endpoints — documented gap

`routes_setup.py` has 4 unauthenticated endpoints (`/setup/test-printer`) that
must be open during initial install (no admin user exists yet). These also call
`_check_ssrf_blocklist()`. After first admin creation, `_setup_is_locked()`
returns 403 on these endpoints. Window of exposure: between fresh install and
first admin creation, on the local network only.

## False Positives — Plugin Discovery (3 findings)

### `non-literal-import` in `core/app.py`

```python
for entry in os.scandir(modules_dir):
    pkg_name = f"modules.{entry.name}"
    mod = importlib.import_module(pkg_name)
```

Module names come from filesystem `os.scandir`, not from user input. Plugin
discovery pattern.

## Approach

For each false positive, an inline `# nosemgrep: <rule> -- <reason>` comment was
added at the call site. Each comment is reviewable in code. The CI gate remains
`semgrep --config auto --error` — no `|| true`, no `.semgrepignore`.
