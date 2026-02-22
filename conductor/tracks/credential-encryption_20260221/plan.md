# Plan: Credential Encryption — SMTP, MQTT, camera_url

## Summary

Three surgical changes across four files to encrypt credentials at rest and stop persisting plaintext RTSP URLs.

## Tasks

### Task 1: SMTP password encryption — write path (`backend/routers/alerts.py`)

**File:** `backend/routers/alerts.py`
**Function:** `update_smtp_config` (line ~524)

In `update_smtp_config`, before saving `smtp_data` to the DB, encrypt the password if one was provided.

Current code:
```python
if not smtp_data.get("password") and config and config.value.get("password"):
    smtp_data["password"] = config.value["password"]
```

Add after that block:
```python
if smtp_data.get("password") and not crypto.is_encrypted(smtp_data["password"]):
    import crypto as _crypto
    smtp_data["password"] = _crypto.encrypt(smtp_data["password"])
```

Also add `import crypto` at the top of the router (it's not currently imported there).

- [x] Add `import crypto` to `routers/alerts.py`
- [x] Encrypt SMTP password on write

---

### Task 2: SMTP password decryption — read path (`backend/alert_dispatcher.py`)

**File:** `backend/alert_dispatcher.py`
**Function:** `_get_smtp_config` (line ~187)

After reading the smtp config from DB, decrypt the password before returning it for use.

```python
def _get_smtp_config(db):
    ...
    smtp = config.value
    if not smtp.get("enabled") or not smtp.get("host"):
        return None
    # Decrypt password (migration-safe: decrypt() falls back to raw on failure)
    if smtp.get("password"):
        try:
            import crypto
            smtp = dict(smtp)  # copy to avoid mutating cached ORM value
            smtp["password"] = crypto.decrypt(smtp["password"])
        except Exception:
            pass
    return smtp
```

- [x] Decrypt SMTP password on read in alert_dispatcher

---

### Task 3: SMTP password decryption — report_runner (`backend/report_runner.py`)

**File:** `backend/report_runner.py`
**Function:** `get_smtp_config` (line ~37)

Same pattern as above after reading config from DB:

```python
def get_smtp_config(session):
    ...
    if not config.get("enabled") or not config.get("host"):
        return None
    # Decrypt password (migration-safe)
    if config.get("password"):
        try:
            import crypto
            config = dict(config)
            config["password"] = crypto.decrypt(config["password"])
        except Exception:
            pass
    return config
```

Note: `report_runner.py` runs as a separate process so it needs its own import.

- [x] Decrypt SMTP password on read in report_runner

---

### Task 4: MQTT republish password encryption — write path (`backend/routers/system.py`)

**File:** `backend/routers/system.py`
**Function:** `update_mqtt_republish_config` (line ~1054)

Currently the password is stored as-is. Add encryption on write:

In the loop `for short_key, value in body.items()`, after the masked-value skip, encrypt the password:

```python
if short_key == "password" and value and value != "••••••••":
    str_val = crypto.encrypt(str(value))
else:
    str_val = str(value).lower() if isinstance(value, bool) else str(value)
```

Replace the generic `str_val` assignment for the password case.

`crypto` is already imported at the top of `system.py` (`import crypto`).

- [x] Encrypt MQTT republish password on write

---

### Task 5: MQTT republish password decryption — read path (`backend/routers/system.py`)

**File:** `backend/routers/system.py`
**Function:** `get_mqtt_republish_config` (line ~1025)

When reading the password key, decrypt before returning the masked value check:

```python
elif short_key == "password":
    # Decrypt for internal check, then mask for response
    raw = val
    try:
        raw = crypto.decrypt(val)
    except Exception:
        pass
    config[short_key] = "••••••••" if raw else ""
```

- [x] Decrypt MQTT password on GET (just to confirm it's set, then mask)

---

### Task 6: MQTT republish password decryption — mqtt_republish.py read path

**File:** `backend/mqtt_republish.py`
**Function:** `_get_config` (line ~38)

After reading `mqtt_republish_password` from DB, decrypt it:

```python
_config_cache = {
    ...
    "password": rows.get("mqtt_republish_password", ""),
    ...
}
# Decrypt password (migration-safe)
try:
    import crypto
    raw_pw = _config_cache["password"]
    if raw_pw:
        _config_cache["password"] = crypto.decrypt(raw_pw)
except Exception:
    pass
```

- [x] Decrypt MQTT password before use in mqtt_republish

---

### Task 7: Stop persisting auto-generated Bambu RTSP URLs (`backend/routers/printers.py`)

**File:** `backend/routers/printers.py`
**Function:** `sync_go2rtc_config` (line ~160)

Remove the lines that persist auto-generated camera URLs back to DB:

```python
# REMOVE these lines:
if not p.camera_url and url:
    p.camera_url = url
    p.camera_discovered = True
```

The `get_camera_url()` function already generates the URL on-demand from the encrypted `api_key`, so there is no need to persist it. The generated URL would contain the plaintext RTSP credential.

For manually-entered camera URLs with embedded credentials (user writes `rtsps://user:pass@...` to `camera_url`): encrypt before storage.

In `create_printer` and `update_printer`, after `_validate_camera_url` is called, add encryption if the URL contains credentials:

```python
# After: printer.camera_url = _validate_camera_url(printer.camera_url)
if printer.camera_url and '@' in printer.camera_url:
    printer.camera_url = crypto.encrypt(printer.camera_url)
```

And in `get_camera_url`, decrypt camera_url if it's encrypted before returning:

```python
def get_camera_url(printer):
    if printer.camera_url:
        url = printer.camera_url
        try:
            url = crypto.decrypt(url)
        except Exception:
            pass
        return url
    ...
```

- [x] Remove auto-persist of RTSP URLs from sync_go2rtc_config
- [x] Encrypt user-supplied camera URLs with credentials on write
- [x] Decrypt camera_url on read in get_camera_url

---

## Acceptance criteria

- [ ] SMTP password encrypted in DB
- [ ] SMTP password decrypted correctly for email sending
- [ ] SMTP GET response masks the password (already done — `password_set` bool field)
- [ ] MQTT republish password encrypted in DB
- [ ] Auto-generated Bambu RTSP URLs not persisted to camera_url column
- [ ] Existing manually-entered camera URLs with credentials encrypted or handled
- [ ] Migration-safe: existing plaintext values work after upgrade
- [ ] `make test` passes (839+ tests)
