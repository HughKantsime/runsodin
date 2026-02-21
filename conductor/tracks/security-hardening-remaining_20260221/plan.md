# Plan: Security Hardening — Remaining Quick Fixes

**Track ID:** security-hardening-remaining_20260221
**Type:** infrastructure
**Created:** 2026-02-21

---

## Overview

7 targeted security hardening fixes with no architectural dependencies. All changes are contained to `docker/entrypoint.sh`, `backend/schemas.py`, `backend/routers/printers.py`, `backend/routers/alerts.py`, `backend/routers/orgs.py`, and `backend/routers/auth.py`. No new tables, no migrations, no frontend changes.

---

## DAG

```
Task 1 (entrypoint.sh JWT entropy)   ─┐
Task 2 (schemas numeric bounds)       ─┤─→ Task 7 (GDPR export) → commit
Task 3 (camera URL validation)        ─┤
Task 4 (webhook SSRF validation)      ─┤
Task 5 (audit: password change)       ─┤
Task 6 (audit: login)                 ─┘
```

Tasks 1–6 are independent and can be implemented sequentially in a single pass. Task 7 reads the same `auth.py` file as Task 5/6 so it goes last in that file.

---

## Tasks

### Task 1: JWT Secret Entropy Fix (`docker/entrypoint.sh`)

**File:** `/Users/shanesmith/Documents/Claude/odin/docker/entrypoint.sh`

**Current code (line 26):**
```sh
export JWT_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
```

**Replace with:**
```sh
export JWT_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_bytes(32).hex())")
```

Note: `ENCRYPTION_KEY` uses `Fernet.generate_key()` which is already cryptographically sound — no change needed there.

- [x] Task 1 complete

---

### Task 2: Numeric Field Bounds in Pydantic Schemas (`backend/schemas.py`)

**File:** `/Users/shanesmith/Documents/Claude/odin/backend/schemas.py`

**Changes needed:**

1. `PrinterBase.slot_count` — already has `ge=1, le=16`. No change needed.

2. `PrinterUpdate.slot_count` (line 118) — currently `Optional[int] = None` with no bounds.
   Replace with: `slot_count: Optional[int] = Field(default=None, ge=1, le=256)`

3. `ModelBase.quantity_per_bed` (line 183) — currently `Optional[int] = 1` with no bounds.
   Replace with: `quantity_per_bed: Optional[int] = Field(default=1, ge=1, le=10000)`

4. `ModelBase.units_per_bed` (line 182) — currently `Optional[int] = 1` with no bounds.
   Replace with: `units_per_bed: Optional[int] = Field(default=1, ge=1, le=10000)`

5. `ModelUpdate.quantity_per_bed` (line 202) — same issue.
   Replace with: `quantity_per_bed: Optional[int] = Field(default=1, ge=1, le=10000)`

6. `ModelUpdate.units_per_bed` (line 201) — same issue.
   Replace with: `units_per_bed: Optional[int] = Field(default=1, ge=1, le=10000)`

7. `JobBase.quantity` (line 250) — already has `ge=1`. Add upper bound.
   Replace with: `quantity: int = Field(default=1, ge=1, le=10000)`

8. `JobBase.priority` (line 251) — currently `Union[int, str]` with no bounds. The `normalize_priority` validator coerces strings, so we leave the type union but note that integer values should be 0–10. Since this field accepts strings via validator, we add a `field_validator` for int range.
   Add after `normalize_priority` validator in `JobResponse`:
   ```python
   @field_validator('priority', mode='before')
   @classmethod
   def clamp_priority(cls, v):
       if isinstance(v, int):
           return max(0, min(10, v))
       return v
   ```
   Actually: for `JobBase`, change `priority` to keep `Union[int, str]` but add a validator in `JobBase` directly. The spec says 0–10. Add validator to `JobBase`:
   ```python
   @field_validator('priority', mode='before')
   @classmethod
   def normalize_priority_base(cls, v):
       if isinstance(v, int):
           if v < 0 or v > 10:
               raise ValueError('priority must be between 0 and 10')
       return v
   ```

- [x] Task 2 complete

---

### Task 3: Camera URL Validation Before go2rtc Config Write

**File:** `/Users/shanesmith/Documents/Claude/odin/backend/routers/printers.py`

Camera URLs are written to go2rtc YAML in `sync_go2rtc_config()`. The `camera_url` field is saved to `Printer.camera_url` via the printer create/update routes and via auto-discovery.

**Add a validation helper function** (near `_check_ssrf_blocklist`):

```python
_CAMERA_SHELL_METACHAR_RE = re.compile(r'[;&|$`\\]')

def _validate_camera_url(url: str) -> str:
    """Validate and sanitize a camera URL before writing to go2rtc config.

    - Only rtsp:// or rtsps:// schemes allowed
    - Strip shell metacharacters
    - Reject localhost/loopback targets
    Returns sanitized URL or raises HTTPException.
    """
    if not url:
        return url

    # Scheme check
    lower = url.strip().lower()
    if not (lower.startswith("rtsp://") or lower.startswith("rtsps://")):
        raise HTTPException(status_code=400, detail="Camera URL must use rtsp:// or rtsps:// scheme")

    # Strip shell metacharacters
    sanitized = _CAMERA_SHELL_METACHAR_RE.sub('', url.strip())

    # SSRF check: extract host from URL
    import urllib.parse
    try:
        parsed = urllib.parse.urlparse(sanitized)
        host = parsed.hostname or ""
        _check_ssrf_blocklist(host)
    except HTTPException:
        raise HTTPException(status_code=400, detail="Camera URL points to a blocked host")

    return sanitized
```

**Apply validation in create_printer and update_printer routes:**

In `create_printer`, after the existing `_check_ssrf_blocklist(printer.api_host)` call, add:
```python
if printer.camera_url:
    printer.camera_url = _validate_camera_url(printer.camera_url)
```

In `update_printer`, when `updates.camera_url` is present, apply the same validation.

**Apply in sync_go2rtc_config:** The `camera_url` stored in DB may have been set before this fix. In `sync_go2rtc_config`, when reading `url = get_camera_url(p)`, add a guard:
- If URL came from `p.camera_url` (manually set), it was already validated at write time (after this fix).
- Bambu-derived URLs come from `get_camera_url()` template string — they inherit the host from `printer.api_host` which was already SSRF-checked. No additional validation needed in sync.

- [x] Task 3 complete

---

### Task 4: Webhook URL SSRF Validation

**Files:**
- `/Users/shanesmith/Documents/Claude/odin/backend/routers/alerts.py` (system webhooks)
- `/Users/shanesmith/Documents/Claude/odin/backend/routers/orgs.py` (org-level webhook URL)

**Add a shared SSRF validator for URLs** in `deps.py`:

```python
import ipaddress as _ipaddress
import urllib.parse as _urllib_parse

def _validate_webhook_url(url: str) -> None:
    """Validate a webhook URL is not targeting internal infrastructure (SSRF prevention).

    Raises HTTPException 400 if URL is invalid or targets loopback/private ranges.
    """
    if not url:
        return
    try:
        parsed = _urllib_parse.urlparse(url)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid webhook URL")

    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Webhook URL must use http:// or https:// scheme")

    host = parsed.hostname or ""
    blocked_prefixes = ("localhost", "127.", "169.254.", "0.", "::1")
    if any(host.startswith(p) for p in blocked_prefixes):
        raise HTTPException(status_code=400, detail="Webhook URL targets a blocked host")

    try:
        addr = _ipaddress.ip_address(host)
        if addr.is_loopback or addr.is_link_local or addr.is_private:
            raise HTTPException(status_code=400, detail="Webhook URL targets a blocked host")
    except ValueError:
        pass  # hostname — allow (DNS resolution at dispatch time)
```

**In `routers/alerts.py` `create_webhook`:** After extracting `url`, add:
```python
from deps import _validate_webhook_url
_validate_webhook_url(url)
```

**In `routers/alerts.py` `update_webhook`:** When `url` is in `data`, add:
```python
if "url" in data:
    from deps import _validate_webhook_url
    _validate_webhook_url(data["url"])
```

**In `routers/orgs.py` `update_org_settings`:** When `webhook_url` is in `body`, add:
```python
if "webhook_url" in body and body["webhook_url"]:
    from deps import _validate_webhook_url
    _validate_webhook_url(body["webhook_url"])
```

Note: Import `_validate_webhook_url` at the top of each router, not inline, for cleanliness.

- [x] Task 4 complete

---

### Task 5: Audit Log on Password Changes (`backend/routers/auth.py`)

**File:** `/Users/shanesmith/Documents/Claude/odin/backend/routers/auth.py`

**Location:** `update_user` function (around line 1038).

Current code after the password update:
```python
    if updates:
        set_clause = ", ".join(f"{k} = :{k}" for k in updates.keys())
        updates['id'] = user_id
        db.execute(text(f"UPDATE users SET {set_clause} WHERE id = :id"), updates)
        db.commit()
    return {"status": "updated"}
```

**Change to:**
```python
    password_changed = 'password_hash' in updates
    if updates:
        set_clause = ", ".join(f"{k} = :{k}" for k in updates.keys())
        updates['id'] = user_id
        db.execute(text(f"UPDATE users SET {set_clause} WHERE id = :id"), updates)
        db.commit()
    if password_changed:
        log_audit(db, "user.password_changed", "user", user_id,
                  {"actor_user_id": current_user["id"], "target_user_id": user_id})
    return {"status": "updated"}
```

- [x] Task 5 complete

---

### Task 6: Audit Log on Successful Logins (`backend/routers/auth.py`)

**File:** `/Users/shanesmith/Documents/Claude/odin/backend/routers/auth.py`

**Location:** `login` function (around line 60).

Current code after MFA check:
```python
    access_token = create_access_token(data={"sub": user.username, "role": user.role})
    _record_session(db, user.id, access_token, client_ip, request.headers.get("user-agent", ""))
    return {"access_token": access_token, "token_type": "bearer"}
```

**Change to:**
```python
    access_token = create_access_token(data={"sub": user.username, "role": user.role})
    _record_session(db, user.id, access_token, client_ip, request.headers.get("user-agent", ""))
    log_audit(db, "auth.login", "user", user.id,
              {"username": user.username, "ip_address": client_ip})
    return {"access_token": access_token, "token_type": "bearer"}
```

Note: `log_audit` already accepts `ip` as a parameter but the `AuditLog` model stores `ip_address`. Pass IP in `details` dict for consistency with the audit log pattern used elsewhere, and also pass via `ip=` kwarg so it gets stored in the dedicated column:

```python
    log_audit(db, "auth.login", "user", user.id,
              details={"username": user.username},
              ip=client_ip)
```

- [x] Task 6 complete

---

### Task 7: GDPR Export Completeness (`backend/routers/auth.py`)

**File:** `/Users/shanesmith/Documents/Claude/odin/backend/routers/auth.py`

**Location:** `export_user_data` function (around line 574).

The current export already includes `active_sessions`. It needs to add:
- API tokens (name, created_at, last_used_at, scopes — NOT token_hash or token_prefix that reveals token)
- quota_usage records

**Current export dict:**
```python
    export = {
        "exported_at": ...,
        "user": u,
        "jobs_submitted": jobs,
        "audit_log_entries": audit,
        "active_sessions": sessions_data,
        "alert_preferences": prefs,
    }
```

**Add before the export dict:**
```python
    api_tokens_data = [dict(r._mapping) for r in db.execute(
        text("SELECT id, name, scopes, created_at, last_used_at, expires_at FROM api_tokens WHERE user_id = :uid"),
        {"uid": user_id}).fetchall()]

    quota_data = [dict(r._mapping) for r in db.execute(
        text("SELECT period_key, grams_used, hours_used, jobs_used, updated_at FROM quota_usage WHERE user_id = :uid"),
        {"uid": user_id}).fetchall()]
```

**Update export dict to include them:**
```python
    export = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "user": u,
        "jobs_submitted": jobs,
        "audit_log_entries": audit,
        "active_sessions": sessions_data,
        "alert_preferences": prefs,
        "api_tokens": api_tokens_data,
        "quota_usage": quota_data,
    }
```

Note: `active_sessions` already omits `token_jti` (the session token) — it selects only `id, ip_address, user_agent, created_at, last_seen_at`. This is correct.

- [x] Task 7 complete

---

## Commit Plan

Single commit after all 7 tasks:

```
feat(security): complete remaining audit hardening quick-fixes

- JWT secret uses token_bytes(32).hex() for full 256-bit entropy
- Pydantic numeric bounds: slot_count, quantity, units_per_bed, quantity_per_bed
- Camera URL validation: rtsp/rtsps scheme only, shell metachar strip, SSRF block
- Webhook URL SSRF validation in create/update_webhook and update_org_settings
- Audit log on password changes (user.password_changed)
- Audit log on successful logins (auth.login with IP)
- GDPR export includes api_tokens and quota_usage records
```

Then bump version: `make bump VERSION=1.3.48`

---

## Acceptance Criteria Checklist

- [ ] entrypoint.sh JWT uses token_bytes(32).hex()
- [ ] slot_count (PrinterUpdate), quantity, units_per_bed, quantity_per_bed have Field bounds
- [ ] Camera create/update rejects non-rtsp schemes with 400
- [ ] Camera URLs pointing to localhost/loopback rejected with 400
- [ ] Shell metacharacters stripped from camera URL before storage
- [ ] Webhook create/update rejects loopback/private URLs with 400
- [ ] Org webhook_url setting validates SSRF
- [ ] log_audit("user.password_changed") called when password updated
- [ ] log_audit("auth.login") called with IP on successful login
- [ ] GDPR export includes api_tokens (no token values) and quota_usage
