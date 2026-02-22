# Spec: Credential Encryption — SMTP, MQTT, camera_url

## Goal

Encrypt the two credential types currently stored in plaintext that were missed in previous hardening passes: SMTP password and MQTT republish password. Also fix the camera_url RTSP credential storage pattern.

## Items

### 1. SMTP Password Encryption (`backend/routers/alerts.py`, `backend/alert_dispatcher.py`, `backend/report_runner.py`)

The SMTP config is stored as a JSON blob in `system_config` under key `smtp_config`. The password field is stored plaintext.

**Write path** (`routers/alerts.py` — `update_smtp_config` or equivalent):
- Before saving, if `smtp_data["password"]` is non-empty, encrypt it: `smtp_data["password"] = crypto.encrypt(smtp_data["password"])`
- If the password field is blank (user didn't change it), preserve the existing encrypted value from DB — this pattern already exists in the code

**Read path** (`alert_dispatcher.py` and `report_runner.py`):
- After reading `smtp_config` from DB, decrypt the password before use: `smtp_config["password"] = crypto.decrypt(smtp_config["password"])`
- Handle the case where the value may still be plaintext (during migration — wrap in try/except, fall back to raw value if decryption fails, so existing deployments don't break on upgrade)

**GET response** (`routers/alerts.py` — when returning smtp_config to frontend):
- Mask the password in responses: return `"••••••••"` or omit the field entirely if a password is set

### 2. MQTT Republish Password Encryption (`backend/routers/system.py`)

The MQTT republish config is stored in `system_config` under key `mqtt_republish_*` or similar. The password is written as a raw string.

- Encrypt on write, decrypt on read, same pattern as SMTP
- Mask in GET responses

### 3. camera_url — Stop Persisting Auto-Generated RTSP Credentials (`backend/routers/printers.py`)

For Bambu printers, `get_camera_url()` generates `rtsps://bblp:<access_code>@<ip>:322/streaming/live/1` and this gets persisted to `camera_url` at startup/discovery. The `access_code` is already stored encrypted in `api_key`. This creates a plaintext copy of the credential.

**Fix:** In the code path that writes the auto-generated camera URL to `p.camera_url` (look for `p.camera_url = url` where `url` comes from `get_camera_url()`):
- Do NOT persist auto-generated RTSP URLs to the DB
- Instead, generate the URL on-demand in `get_camera_url()` each time it's called (it already has access to the decrypted `api_key`)
- For manually-entered camera URLs (user-supplied RTSP URLs with embedded credentials): if the URL contains credentials (`rtsps://user:pass@...`), encrypt the entire URL with Fernet before storage and decrypt on read

The `sanitize_camera_url()` validator on `PrinterResponse` already strips credentials from API responses — that part is correct and should remain.

## Acceptance Criteria

- [ ] SMTP password encrypted in DB (verified by checking `system_config` table after saving SMTP config)
- [ ] SMTP password decrypted correctly for email sending
- [ ] SMTP GET response masks the password
- [ ] MQTT republish password encrypted in DB
- [ ] Auto-generated Bambu RTSP URLs not persisted to `camera_url` column
- [ ] Existing manually-entered camera URLs with credentials encrypted or handled
- [ ] Migration-safe: existing plaintext values work after upgrade (try/except decrypt fallback)
- [ ] `make test` passes

## Technical Notes

- `crypto.encrypt()` and `crypto.decrypt()` are in `backend/crypto.py`; import with `from crypto import encrypt, decrypt` or `from crypto import Crypto; c = Crypto()`
- Look at OIDC client secret handling in `routers/auth.py:~1063` as the exact pattern to replicate
- The SMTP config write endpoint is in `routers/alerts.py` — search for `smtp_config` writes
- The MQTT republish config — search `system.py` for `mqtt_republish` or `mqtt` in system_config writes
- Migration fallback: `try: value = crypto.decrypt(raw) except Exception: value = raw` — this means first-run after upgrade continues to work
